#!/usr/bin/env python3
"""
core.py — the shared memory layer over Qdrant.

Both mem_fusion.py (stdio MCP server, serving Claude) and constellation.py
(HTTP server, serving peers) are thin proxies on top of these functions.
This is the only module that talks to Qdrant.

Functions return plain dicts; transport-specific serialization (MCP JSON,
HTTP JSON, etc.) is the caller's responsibility.

If Ollama is unreachable when embedding is required, returns
{"error": "ollama_unreachable", "detail": ...}. If Ollama is reachable but
rejects the input (e.g., HTTP 500 because the content exceeds the model's
context window), returns {"error": "embed_failed", "reason": ...}. Other
unexpected failures return {"error": "embed_unexpected", ...}. Callers
surface the error verbatim. No silent queueing.

Inputs exceeding EMBED_MAX_CHARS are soft-truncated before embedding; the
caller's stored payload retains the full content, only the vector is
derived from the truncated text.
"""
import hashlib
import json
import logging
import os
import socket
import unicodedata
import uuid
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import (
    DatetimeRange, FieldCondition, Filter, MatchValue, PointStruct, Range,
)

# ── Constants (shared by mem_fusion.py and constellation.py) ──────────────
QDRANT_URL  = os.getenv("QDRANT_URL",  "http://127.0.0.1:6333")
OLLAMA_URL  = os.getenv("OLLAMA_URL",  "http://127.0.0.1:11434")
# The collection name is invariant. Every entry carries a `groups` payload
# (list[str]); routing happens by group membership, not by collection name.
COLLECTION  = "cowork_memories"
EMBED_MODEL = "nomic-embed-text"
VECTOR_SIZE = 768
# nomic-embed-text has a ~2048-token context window. ~8000 chars is the
# practical char-equivalent ceiling; inputs beyond this are soft-truncated
# before embedding (the full content is preserved in the stored payload).
EMBED_MAX_CHARS = 8000
# In-process LRU cache of embeddings, keyed by SHA-256 of the truncated
# text actually sent to Ollama. Eliminates re-embedding cost on repeat
# queries (session-start primers, identical search_memory calls, etc.).
# 1000 entries × 3 KB/vector ≈ 3 MB RAM. Capacity bound; oldest evicted.
EMBED_CACHE_CAPACITY = 1000
_embed_cache: OrderedDict = OrderedDict()

CONSTELLATION_CONFIG_PATH = Path(os.getenv(
    "MEMFUSION_CONSTELLATION_CONFIG",
    str(Path.home() / ".local/share/mem-fusion/constellation/config.json"),
))


def _resolve_node_name() -> str:
    """Resolve the local peer identity for origin_node tagging.

    Precedence:
      1. MEMFUSION_NODE_NAME env var (explicit, used by tests + custom installs)
      2. node_name from Constellation's config.json if present (single source
         of truth for peer identity when Constellation is installed)
      3. socket.gethostname() — safe fallback for single-node installs
    """
    if name := os.getenv("MEMFUSION_NODE_NAME"):
        return name
    if CONSTELLATION_CONFIG_PATH.exists():
        try:
            with open(CONSTELLATION_CONFIG_PATH) as f:
                cfg = json.load(f)
            if name := cfg.get("node_name"):
                return name
        except Exception:
            pass
    return socket.gethostname()


# NODE_NAME snapshot at module-load time — preserved for diagnostics and
# back-compat. WRITE PATHS (store_memory, find_or_create, etc.) MUST call
# _resolve_node_name() on every store so a freshly-installed Constellation
# config takes effect without restarting the MCP server. See fix 0.5.0-012.
NODE_NAME = _resolve_node_name()

DEFAULT_GROUPS = ["personal"]

LOG_DIR     = os.getenv("MEMFUSION_LOG_DIR",
                        str(Path.home() / ".local/share/mem-fusion/logs"))

# Set by configure_logging() — the per-startup log file the current process
# is writing to. Daemons print this on startup so users know where to tail.
ACTIVE_LOG_PATH: str | None = None


# ── Per-startup logger (one file per daemon run, component-prefixed) ─────
def configure_logging(component: str) -> logging.Logger:
    """Configure logging for the calling process.

    Each daemon startup writes to its own file:
      <LOG_DIR>/<component>.<UTC-timestamp>.log

    The [component] prefix in the formatter keeps the file content
    self-describing even if multiple files are concatenated for cross-daemon
    debugging. Each process calls this once at startup.
    """
    global ACTIVE_LOG_PATH
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    ACTIVE_LOG_PATH = str(Path(LOG_DIR) / f"{component}.{ts}.log")
    root = logging.getLogger()
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        handler = logging.FileHandler(ACTIVE_LOG_PATH)
        handler.setFormatter(logging.Formatter(
            f"%(asctime)s %(levelname)-5s [{component:<14}] %(message)s"
        ))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    return logging.getLogger(component)


# Module-level logger. Each importing process should also call
# configure_logging(component) to add the file handler exactly once.
log = logging.getLogger("core")


# ── Qdrant client (module-level singleton) ─────────────────────────────────
qdrant = QdrantClient(url=QDRANT_URL, timeout=10)


# ── Hashing + timestamps ──────────────────────────────────────────────────
def content_hash(text: str) -> str:
    """Canonical content hash per docs/v0.5_CONTENT_HASH_SPEC.md.

    Algorithm: NFC-normalize → encode UTF-8 (no BOM) → SHA-256 → lowercase
    hex, full 64-char digest (no truncation). The dedup primitive; must be
    byte-identical across all mem-fusion implementations.

    Changed in 0.5.0-015 (architect-acked spec). The legacy form was
    `sha256(text.strip().lower())[:16]` — Unicode-naive, case-folded,
    whitespace-stripped, 64-bit truncated. Rationale for each removal
    in the spec §4.1.

    Wire-format companion: SlackConnector._wire_hash returns the same
    digest with a `"sha256:"` prefix (the §5.1 wire serialization).
    Both forms agree byte-for-byte on the 64 hex digits, so a peer's
    received content has identical local hash to whatever the sender
    computed — Unicode drift across editors no longer fragments dedup.
    """
    normalized = unicodedata.normalize("NFC", text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def iso_now() -> str:
    """Microsecond-precision ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


# ── Embedding (Ollama) ─────────────────────────────────────────────────────
async def embed(text: str) -> list[float] | dict:
    """Generate a 768-dim vector via local Ollama.

    Returns either:
      list[float]                          — embedding on success
      {"error": "ollama_unreachable",  …}  — connection failure (Ollama down)
      {"error": "embed_failed",        …}  — Ollama returned non-200 (e.g., 500
                                             for input exceeding context length)
      {"error": "embed_unexpected",    …}  — malformed response or other failure

    Soft-truncates text to EMBED_MAX_CHARS before sending. Caller preserves
    the full content in storage; only the embedding is derived from the
    truncated text.

    Successful embeddings are cached in-process (LRU, capacity
    EMBED_CACHE_CAPACITY). Cache key is the SHA-256 of the truncated text
    actually sent to Ollama, so two callers passing inputs that differ
    only past EMBED_MAX_CHARS share a cache entry (correct: Ollama would
    produce identical vectors for them). Errors are never cached.
    """
    embed_text = text[:EMBED_MAX_CHARS] if len(text) > EMBED_MAX_CHARS else text

    cache_key = hashlib.sha256(embed_text.encode("utf-8")).hexdigest()
    cached = _embed_cache.get(cache_key)
    if cached is not None:
        _embed_cache.move_to_end(cache_key)
        return cached

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/embeddings",
                                  json={"model": EMBED_MODEL, "prompt": embed_text})
            if r.status_code != 200:
                try:
                    reason = r.json().get("error") or r.text[:200]
                except Exception:
                    reason = r.text[:200]
                log.error("embed failed: HTTP %d — %s", r.status_code, reason)
                return {"error": "embed_failed",
                        "detail": f"Ollama returned HTTP {r.status_code}",
                        "reason": reason or "unknown",
                        "status_code": r.status_code}
            data = r.json()
            if "embedding" not in data:
                log.error("embed: unexpected response shape: %s", str(data)[:200])
                return {"error": "embed_unexpected",
                        "detail": "Ollama response missing 'embedding' field",
                        "reason": str(data)[:200]}
            vec = data["embedding"]
            _embed_cache[cache_key] = vec
            if len(_embed_cache) > EMBED_CACHE_CAPACITY:
                _embed_cache.popitem(last=False)  # evict oldest
            return vec
    except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
        log.error("embed: ollama unreachable — %s", e)
        return {"error": "ollama_unreachable",
                "detail": f"Cannot reach Ollama at {OLLAMA_URL}",
                "reason": str(e)}
    except Exception as e:
        log.error("embed: unexpected error — %s", e)
        return {"error": "embed_unexpected",
                "detail": "Unexpected error during embedding",
                "reason": str(e)}


# ── Qdrant filter helpers ─────────────────────────────────────────────────
def entry_groups(payload: dict) -> list[str]:
    """Read a payload's groups, applying v0.3 backward-compat fallback.

    v0.4 entries carry `groups: list[str]`. Legacy v0.3 entries had either
    `source=local` (no group) or `source=group` with a singular `group_name`.
    This helper normalizes both shapes to the v0.4 canonical list form.
    """
    g = payload.get("groups")
    if isinstance(g, list) and g:
        return list(g)
    legacy_group = payload.get("group_name")
    if payload.get("source") == "group" and legacy_group:
        return [legacy_group]
    return list(DEFAULT_GROUPS)


def union_groups(*group_lists) -> list[str]:
    """Order-preserving deduplicated union across one or more group lists."""
    seen, out = set(), []
    for gs in group_lists:
        for g in gs or []:
            if g and g not in seen:
                seen.add(g)
                out.append(g)
    return out


def normalize_groups_arg(groups) -> list[str]:
    """Coerce a caller-supplied groups arg into a clean canonical list."""
    if not isinstance(groups, list) or not groups:
        return list(DEFAULT_GROUPS)
    cleaned = union_groups(groups)
    return cleaned or list(DEFAULT_GROUPS)


# ── v0.5 connector_ids helpers (architecture doc §4) ──────────────────────
# `connector_ids: list[str]` is the v0.5 connector-routing tag. Absent or
# empty → memory is local-only (the new default; the v0.4 `groups: [personal]`
# default was a scope-determination tag and is now ignored per §4 Migration).
# A memory tagged with a connector id becomes eligible for push to THAT
# connector via `/remember push <connector-id>`. Multi-tagging supported;
# pushes are per-connector (push to "engineering" doesn't fan out to
# "marketing"). Union semantics on add (additive only — un-tagging would
# require cross-connector retraction, deferred).
def entry_connector_ids(payload: dict) -> list[str]:
    """Read a payload's connector_ids, defaulting to [] for legacy entries."""
    cids = payload.get("connector_ids")
    return list(cids) if isinstance(cids, list) else []


def normalize_connector_ids_arg(connector_ids) -> list[str]:
    """Coerce a caller-supplied connector_ids arg into a clean canonical list.

    Returns [] for None / non-list / empty input. Unlike groups, the default
    is empty (local-only) — there is no `personal` analog.
    """
    if not isinstance(connector_ids, list) or not connector_ids:
        return []
    return union_groups(connector_ids)  # reuse: order-preserving dedup union


def find_existing_by_hash(chash: str) -> tuple[str, dict] | None:
    """Return (point_id, payload) for an entry with this content_hash, else None."""
    try:
        results, _ = qdrant.scroll(
            collection_name=COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="content_hash", match=MatchValue(value=chash))
            ]),
            limit=1, with_payload=True,
        )
        if results:
            return str(results[0].id), dict(results[0].payload or {})
    except Exception as e:
        log.warning("Duplicate check failed: %s", e)
    return None


def since_to_filter(since: str | None):
    """Parse 'Nh' / 'Nd' / ISO into a Qdrant DatetimeRange filter on 'timestamp'."""
    if not since:
        return None
    try:
        if since.endswith("h"):
            dt = datetime.now(timezone.utc) - timedelta(hours=float(since[:-1]))
        elif since.endswith("d"):
            dt = datetime.now(timezone.utc) - timedelta(days=float(since[:-1]))
        else:
            dt = datetime.fromisoformat(since)
        return FieldCondition(key="timestamp", range=DatetimeRange(gte=dt))
    except Exception:
        return None


def build_filter(project=None, type_=None, since=None, min_importance=1, connector_id=None):
    """Compose a Qdrant Filter from optional fields."""
    conditions = []
    if project:
        conditions.append(FieldCondition(key="project", match=MatchValue(value=project)))
    if type_:
        conditions.append(FieldCondition(key="type", match=MatchValue(value=type_)))
    f = since_to_filter(since)
    if f:
        conditions.append(f)
    if min_importance and min_importance > 1:
        conditions.append(FieldCondition(key="importance", range=Range(gte=min_importance)))
    if connector_id:
        # Qdrant keyword-list match: includes any point whose connector_ids
        # list contains the given value. Indexed via the `connector_ids`
        # payload index (added at init_collection time).
        conditions.append(FieldCondition(key="connector_ids", match=MatchValue(value=connector_id)))
    return Filter(must=conditions) if conditions else None


def format_results(hits):
    """Shape Qdrant hits into the wire-friendly result format."""
    return [{
        "id":             str(h.id),
        "score":          round(h.score, 4),
        "content":        h.payload.get("content", ""),
        "type":           h.payload.get("type", ""),
        "project":        h.payload.get("project", ""),
        "tags":           h.payload.get("tags", []),
        "importance":     h.payload.get("importance", 3),
        "timestamp":      h.payload.get("timestamp", ""),
        "origin_node":    h.payload.get("origin_node", ""),
        "received_at":    h.payload.get("received_at", ""),
        "groups":         entry_groups(h.payload or {}),
        "connector_ids":  entry_connector_ids(h.payload or {}),
    } for h in hits]


# ── Memory operations ─────────────────────────────────────────────────────
async def store_memory(args: dict) -> dict:
    """Embed, dedup-merge, insert. Returns one of:
       {status: "stored",    id, groups} — fresh content stored
       {status: "merged",    id, groups} — content_hash hit; groups widened
       {status: "duplicate", id, groups} — content_hash hit; nothing changed
       {error: <class>, detail, ...}     — embed failed; caller surfaces error
                                           (class is one of ollama_unreachable,
                                           embed_failed, embed_unexpected)
    """
    content       = args["content"]
    type_         = args["type"]
    tags          = args.get("tags", [])
    project       = args.get("project", "")
    importance    = int(args.get("importance", 3))
    session_id    = args.get("session_id", "")
    groups        = normalize_groups_arg(args.get("groups"))
    connector_ids = normalize_connector_ids_arg(args.get("connector_ids"))

    chash    = content_hash(content)
    existing = find_existing_by_hash(chash)
    if existing:
        existing_id, payload = existing
        current_g  = entry_groups(payload)
        current_c  = entry_connector_ids(payload)
        merged_g   = union_groups(current_g, groups)
        merged_c   = union_groups(current_c, connector_ids)
        if merged_g == current_g and merged_c == current_c:
            return {"status": "duplicate", "id": existing_id,
                    "groups": current_g, "connector_ids": current_c}
        payload_update = {"groups": merged_g, "connector_ids": merged_c}
        qdrant.set_payload(collection_name=COLLECTION,
                           payload=payload_update, points=[existing_id])
        return {"status": "merged", "id": existing_id,
                "groups": merged_g, "connector_ids": merged_c}

    vec = await embed(content)
    if isinstance(vec, dict):
        return vec  # error from embed() — propagate verbatim (preserves error class)

    point_id = str(uuid.uuid4())
    ts       = iso_now()
    qdrant.upsert(collection_name=COLLECTION, points=[PointStruct(
        id=point_id, vector=vec,
        payload={
            "content": content, "type": type_, "tags": tags, "project": project,
            "importance": importance, "session_id": session_id,
            "content_hash": chash, "timestamp": ts,
            "groups": groups, "connector_ids": connector_ids,
            "origin_node": _resolve_node_name(), "submitted_at": ts,
        },
    )])
    return {"status": "stored", "id": point_id,
            "groups": groups, "connector_ids": connector_ids}


# ── v0.5 generic envelope ingest (architecture doc §5 + §6.2) ─────────────
# Connector-substrate-agnostic. Operates on the canonical envelope dict
# (whatever shape was extracted by the connector's parse_envelope).
# The Slack-specific format_envelope / parse_envelope live in
# src/slack_connector.py; future connectors (gdrive, teams, ...) each
# ship their own sibling module. Once parsed, the receive flow below is
# the same for every substrate: loopback check, integrity check, dedup,
# vector resolution, Qdrant upsert.
async def store_memory_from_envelope(envelope: dict,
                                     connector_type: str = "slack") -> dict:
    """Receive-side: ingest a memory from a parsed connector envelope.

    SUBSTRATE-AGNOSTIC. The only thing this function knows about a
    specific connector is its `type` discriminator — used to look up the
    Connector implementation that owns the substrate's integrity scheme.
    Per Stage 1.1 (architect-acked staff audit, 2026-05-18), the previous
    hardcoded `"sha256:" + sha256(content).hexdigest()` check has moved
    onto SlackConnector.verify_integrity; future connectors may use
    signed envelopes, HMAC-MAC, or different hash functions — all
    invisible to this function.

    Performs (in order):
      1. Required fields — bail with `missing_field` if absent. This is
         a structural pre-flight; integrity verification handles missing
         CONTENT fields, but having `origin_node` is required for the
         loopback check that runs before integrity.
      2. Loopback prevention — skip if origin_node == self.node_name.
         Runs BEFORE integrity so we don't pay sha256 cost on our own
         posts pulled back from a shared channel.
      3. Wire-format integrity — delegate to `connector.verify_integrity`.
         Catches transport corruption and tampering.
      4. Local dedup — compute local-canonical content_hash and check
         against existing payloads. Returns "duplicate" on hit (with
         additive connector_id widening if the envelope brings new ids).
      5. Vector resolution — use envelope.vector if present and well-formed
         (VECTOR_SIZE list of floats); else re-embed locally via Ollama.
      6. Persist — upsert into Qdrant with received_at set to now and
         timestamp mirroring received_at (so search_recent surfaces freshly
         received entries without code knowing they're remote).

    Returns one of:
      {"status": "stored",           "id": ..., "connector_ids": [...]}
      {"status": "merged",           "id": ..., "connector_ids": [...]}
      {"status": "duplicate",        "id": ...}
      {"status": "loopback_skipped", "detail": ...}
      {"error":  "integrity_failed", "detail": ...}
      {"error":  "missing_field",    "detail": ...}
      {"error":  "unknown_connector_type", "detail": ...}
      {"error":  <embed-error-class>, ...}  — propagated from core.embed()
    """
    # 0. Resolve connector implementation
    try:
        from connectors import get_connector
        connector = get_connector(connector_type)
    except ValueError as e:
        return {"error": "unknown_connector_type",
                "detail": str(e)}

    # 1. Required fields (structural pre-flight; integrity check enforces more)
    if not isinstance(envelope, dict):
        return {"error": "missing_field",
                "detail": f"envelope is not a dict: {type(envelope).__name__}"}
    for required in ("content", "content_hash", "origin_node"):
        if required not in envelope:
            return {"error": "missing_field",
                    "detail": f"envelope missing required field: {required}"}

    content = envelope["content"]
    origin  = envelope["origin_node"]

    # 2. Loopback prevention
    if origin == _resolve_node_name():
        return {"status": "loopback_skipped",
                "detail": f"envelope origin {origin!r} is this peer; skipping"}

    # 3. Wire-format integrity — delegated to connector
    ok, detail = connector.verify_integrity(envelope)
    if not ok:
        return {"error": "integrity_failed", "detail": detail}

    # 4. Local dedup via local-canonical hash
    local_chash = content_hash(content)
    existing = find_existing_by_hash(local_chash)
    if existing:
        existing_id, payload = existing
        # Optional: additively widen connector_ids on the existing entry
        # if the incoming envelope adds new ids. Matches the semantics of
        # `add_connector_ids` for the through-Slack arrival path.
        incoming_cids = envelope.get("connector_ids") or []
        if incoming_cids:
            current_cids = entry_connector_ids(payload)
            merged = union_groups(current_cids, incoming_cids)
            if merged != current_cids:
                qdrant.set_payload(collection_name=COLLECTION,
                                   payload={"connector_ids": merged},
                                   points=[existing_id])
                return {"status": "merged", "id": existing_id,
                        "connector_ids": merged}
        return {"status": "duplicate", "id": existing_id}

    # 5. Vector — use envelope's if shape-correct, else re-embed
    vec_in = envelope.get("vector")
    if isinstance(vec_in, list) and len(vec_in) == VECTOR_SIZE and \
       all(isinstance(x, (int, float)) for x in vec_in):
        vec = vec_in
    else:
        result = await embed(content)
        if isinstance(result, dict):
            return result  # propagate embed error class
        vec = result

    # 6. Persist
    point_id    = str(uuid.uuid4())
    received_at = iso_now()
    payload = {
        "content":       content,
        "content_hash":  local_chash,
        "type":          envelope.get("type", ""),
        "tags":          envelope.get("tags", []),
        "project":       envelope.get("project", ""),
        "importance":    envelope.get("importance", 3),
        "groups":        envelope.get("groups", []),
        "connector_ids": normalize_connector_ids_arg(envelope.get("connector_ids")),
        "origin_node":   origin,
        "submitted_at":  envelope.get("submitted_at", received_at),
        "received_at":   received_at,
        # `timestamp` mirrors `received_at` so search_recent surfaces this
        # entry to the user shortly after arrival, regardless of submitted_at.
        "timestamp":     received_at,
    }
    qdrant.upsert(collection_name=COLLECTION, points=[PointStruct(
        id=point_id, vector=vec, payload=payload,
    )])
    return {"status": "stored", "id": point_id,
            "connector_ids": payload["connector_ids"]}


async def add_groups(args: dict) -> dict:
    """Additive union of group tags on existing entries.

    Inputs:  memory_ids: list[str], groups: list[str]
    Returns: {updated: [{id, groups, added}], no_op: [{id, groups}],
              errors: [{id, reason}]}

    Additive only — never removes a group. Removal is deferred (see v0.4 §11).
    """
    memory_ids = args.get("memory_ids", [])
    incoming   = args.get("groups", [])
    if not isinstance(memory_ids, list) or not memory_ids:
        return {"error": "missing_argument",
                "detail": "memory_ids required (non-empty list[str])"}
    if not isinstance(incoming, list) or not incoming:
        return {"error": "missing_argument",
                "detail": "groups required (non-empty list[str])"}
    incoming = union_groups(incoming)

    updated, no_op, errors = [], [], []
    for mid in memory_ids:
        try:
            points = qdrant.retrieve(collection_name=COLLECTION,
                                     ids=[mid], with_payload=True, with_vectors=False)
        except Exception as e:
            errors.append({"id": mid, "reason": f"retrieve_failed: {e}"})
            continue
        if not points:
            errors.append({"id": mid, "reason": "not_found"})
            continue
        payload = dict(points[0].payload or {})
        current = entry_groups(payload)
        merged  = union_groups(current, incoming)
        if merged == current:
            no_op.append({"id": mid, "groups": current})
            continue
        added = [g for g in incoming if g not in current]
        qdrant.set_payload(collection_name=COLLECTION,
                           payload={"groups": merged}, points=[mid])
        updated.append({"id": mid, "groups": merged, "added": added})

    return {"updated": updated, "no_op": no_op, "errors": errors}


async def add_connector_ids(args: dict) -> dict:
    """Additive union of connector_ids on existing entries.

    Inputs:  memory_ids: list[str], connector_ids: list[str]
    Returns: {updated: [{id, connector_ids, added}],
              no_op:   [{id, connector_ids}],
              errors:  [{id, reason}]}

    Additive only — same shape as add_groups. v0.4 add_groups exists for
    legacy `groups` widening; this is the v0.5 parallel for connector
    routing (architecture doc §4 Migration).

    Use case: store memories with `connector_ids=[]` (local-only default),
    then later say "share with engineering" → Claude calls
    `add_connector_ids(memory_ids=[...], connector_ids=["engineering"])`
    followed by `/remember push engineering` to ship them out.
    """
    memory_ids = args.get("memory_ids", [])
    incoming   = args.get("connector_ids", [])
    if not isinstance(memory_ids, list) or not memory_ids:
        return {"error": "missing_argument",
                "detail": "memory_ids required (non-empty list[str])"}
    if not isinstance(incoming, list) or not incoming:
        return {"error": "missing_argument",
                "detail": "connector_ids required (non-empty list[str])"}
    incoming = union_groups(incoming)  # order-preserving dedup (reused helper)

    updated, no_op, errors = [], [], []
    for mid in memory_ids:
        try:
            points = qdrant.retrieve(collection_name=COLLECTION,
                                     ids=[mid], with_payload=True, with_vectors=False)
        except Exception as e:
            errors.append({"id": mid, "reason": f"retrieve_failed: {e}"})
            continue
        if not points:
            errors.append({"id": mid, "reason": "not_found"})
            continue
        payload = dict(points[0].payload or {})
        current = entry_connector_ids(payload)
        merged  = union_groups(current, incoming)
        if merged == current:
            no_op.append({"id": mid, "connector_ids": current})
            continue
        added = [c for c in incoming if c not in current]
        qdrant.set_payload(collection_name=COLLECTION,
                           payload={"connector_ids": merged}, points=[mid])
        updated.append({"id": mid, "connector_ids": merged, "added": added})

    return {"updated": updated, "no_op": no_op, "errors": errors}


# ── v0.5 per-connector cursor persistence (architecture doc §4 / §6.4) ────
# Per-connector cursor file at ~/.local/share/mem-fusion/connector_cursors.json:
#   {"<connector_id>": "<last_pulled_submitted_at_iso>", ...}
# Read on /remember pull start, written after the pull completes successfully.
# Setting cursor only on success makes pulls interruption-safe — partial
# pulls re-run from "no cursor" state and content_hash dedup absorbs the
# redundant work. See docs/v0.5_FIRST_PULL_SEMANTICS.md §3.6.
CONNECTOR_CURSORS_PATH = Path(os.getenv(
    "MEMFUSION_CONNECTOR_CURSORS",
    str(Path.home() / ".local/share/mem-fusion/connector_cursors.json"),
))


def get_connector_cursor(connector_id: str) -> str | None:
    """Return the persisted cursor (ISO timestamp string) for a connector,
    or None if no cursor has been set yet, the file is missing, the key is
    absent, or the file is corrupt. Never raises on filesystem errors.
    """
    if not connector_id:
        return None
    try:
        if not CONNECTOR_CURSORS_PATH.exists():
            return None
        with open(CONNECTOR_CURSORS_PATH) as f:
            cursors = json.load(f)
        if not isinstance(cursors, dict):
            log.warning("connector_cursors.json is not a dict; ignoring")
            return None
        val = cursors.get(connector_id)
        return val if isinstance(val, str) else None
    except (json.JSONDecodeError, OSError) as e:
        log.warning("get_connector_cursor(%s) failed: %s", connector_id, e)
        return None


def set_connector_cursor(connector_id: str, iso_ts: str) -> None:
    """Persist the cursor for a connector. Atomic via tempfile + os.replace.

    Idempotent: setting the same value is a no-op observationally.
    Concurrent writes to different keys preserve both (read-modify-write
    under the assumption of single-process-per-machine; v0.5 doesn't have
    cross-process concurrent cursor writers).
    """
    if not connector_id or not isinstance(iso_ts, str):
        raise ValueError("connector_id and iso_ts must be non-empty strings")
    CONNECTOR_CURSORS_PATH.parent.mkdir(parents=True, exist_ok=True)
    cursors: dict = {}
    if CONNECTOR_CURSORS_PATH.exists():
        try:
            with open(CONNECTOR_CURSORS_PATH) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cursors = loaded
        except (json.JSONDecodeError, OSError) as e:
            log.warning("set_connector_cursor: starting fresh — %s", e)
    cursors[connector_id] = iso_ts
    # Atomic write: write to temp file in same directory, then os.replace.
    tmp_path = CONNECTOR_CURSORS_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(cursors, f, indent=2)
    os.replace(tmp_path, CONNECTOR_CURSORS_PATH)


# ── v0.5 connector.json schema + envelope rendering (G3, push orchestration) ─

CONNECTORS_CONFIG_PATH = Path(os.getenv(
    "MEMFUSION_CONNECTORS_CONFIG",
    str(Path.home() / ".local/share/mem-fusion/connector.json"),
))

#: Type-specific required fields per connector type, validated in
#: load_connectors_config. Keep in lock-step with src/connectors/_REGISTRY.
_CONNECTOR_TYPE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "slack": ("channel",),
}


def load_connectors_config(path: str | None = None) -> dict:
    """Load and validate ~/.local/share/mem-fusion/connector.json (G3).

    Architect-acked at docs/v0.5_ARCHITECT_ACK_2026-05-17.md §D G3.
    Closes a real foot-gun: malformed configs used to fail at first push
    with confusing TypeErrors deep in MCP plumbing.

    Returns:
      {
        "connectors":  list[dict]  — validated entries (id, type, ...),
        "errors":      list[str]   — blocking errors; non-empty → caller bails,
        "warnings":    list[str]   — non-blocking; caller surfaces,
        "config_path": str,
      }

    Validation:
      - File exists + parses as JSON.
      - Top-level shape is {"connectors": list}.
      - Each entry is a dict.
      - `id` (str, non-empty) + `type` (str, non-empty) required.
      - `id` unique within the list.
      - `type` is a known connector type (currently: "slack").
      - Type-specific required fields present per
        _CONNECTOR_TYPE_REQUIRED_FIELDS.

    Optional fields tolerated (per architect ack §B.1):
      - `first_pull_max_age_days: int | None` — first-pull cap knob.

    Never raises — returns errors in the dict instead, so callers can
    render them to the user without catching exceptions.
    """
    cfg_path = Path(path) if path else CONNECTORS_CONFIG_PATH
    out = {
        "connectors":  [],
        "errors":      [],
        "warnings":    [],
        "config_path": str(cfg_path),
    }
    if not cfg_path.exists():
        out["errors"].append(
            f"connector.json not found at {cfg_path}. "
            f"Create it with at least one connector entry, e.g.: "
            f'{{"connectors": [{{"id": "engineering", "type": "slack", "channel": "C0XXXXXXX"}}]}}'
        )
        return out
    try:
        with open(cfg_path) as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        out["errors"].append(f"connector.json is not valid JSON: {e}")
        return out
    except OSError as e:
        out["errors"].append(f"connector.json could not be read: {e}")
        return out

    if not isinstance(raw, dict):
        out["errors"].append(
            f"connector.json must be a JSON object with a 'connectors' list; "
            f"got {type(raw).__name__}"
        )
        return out
    entries = raw.get("connectors")
    if not isinstance(entries, list):
        out["errors"].append(
            "connector.json missing or invalid 'connectors' list; expected "
            "{\"connectors\": [{...}, ...]}"
        )
        return out

    seen_ids: set[str] = set()
    from connectors import list_connector_types
    known_types = set(list_connector_types())
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            out["errors"].append(f"connectors[{idx}]: must be an object, got {type(entry).__name__}")
            continue
        # id
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            out["errors"].append(f"connectors[{idx}]: 'id' must be a non-empty string")
            continue
        if entry_id in seen_ids:
            out["errors"].append(f"connectors[{idx}]: duplicate id {entry_id!r}")
            continue
        seen_ids.add(entry_id)
        # type
        entry_type = entry.get("type")
        if not isinstance(entry_type, str) or not entry_type:
            out["errors"].append(f"connectors[{idx}] ({entry_id!r}): 'type' must be a non-empty string")
            continue
        if entry_type not in known_types:
            out["errors"].append(
                f"connectors[{idx}] ({entry_id!r}): unknown type {entry_type!r}; "
                f"available: {sorted(known_types)}"
            )
            continue
        # type-specific required fields
        missing = [f for f in _CONNECTOR_TYPE_REQUIRED_FIELDS.get(entry_type, ())
                   if not isinstance(entry.get(f), str) or not entry.get(f)]
        if missing:
            out["errors"].append(
                f"connectors[{idx}] ({entry_id!r}, type={entry_type!r}): "
                f"missing required field(s): {missing}"
            )
            continue
        out["connectors"].append(entry)
    return out


async def load_connectors_config_tool(args: dict) -> dict:
    """MCP tool wrapper for load_connectors_config (G3).

    Renders the same structure synchronously — wrapped async only because
    the MCP dispatch table is async-uniform. Argument `path` is optional
    and used for tests; production callers omit and get the default
    ~/.local/share/mem-fusion/connector.json.
    """
    return load_connectors_config(args.get("path"))


async def build_connector_envelope(args: dict) -> dict:
    """Compose a connector-substrate wire payload from a local memory.

    The single-call primitive that powers /remember push <connector-id>
    orchestration. Combines: load_connectors_config → export_record →
    Connector.build_envelope_from_record → Connector.format_envelope.
    Returns everything the skill needs to call the substrate's send-
    message MCP tool (Slack: `slack_send_message`).

    Inputs (args):
      memory_id    — id of a local memory (from export_record / search)
      connector_id — connector entry's id field in connector.json

    Returns one of:
      {
        body:         str,    # the rendered message body for slack_send_message
        channel:      str,    # `channel` field from connector.json (may be name
                              # or id; caller resolves names via slack_search_channels)
        connector_id: str,    # echo of input
        type:         str,    # connector type (e.g., "slack")
        submitted_at: str,    # the memory's submitted_at (for cursor advancement)
        envelope:     dict,   # full envelope dict (for diagnostics / logs)
        body_size:    int,    # len(body) in chars
      }

      {error: "connector_config_invalid",     detail: [str], ...}
      {error: "connector_not_found",          detail: str, available: [str]}
      {error: "memory_not_found",             detail: str}
      {error: "build_envelope_failed",        detail: str}     # missing origin_node etc.
      {error: "body_too_large_for_substrate", detail: str, size: int, limit: int}

    NOT exposed: a "push" tool that calls Slack. The skill orchestrates
    `slack_send_message` separately so connector code never depends on
    the Slack MCP layer (per architecture doc §6 — orchestration in the
    skill, not in mem-fusion's process).
    """
    memory_id    = args.get("memory_id")
    connector_id = args.get("connector_id")
    if not isinstance(memory_id, str) or not memory_id:
        return {"error": "missing_argument",
                "detail": "memory_id required (non-empty string)"}
    if not isinstance(connector_id, str) or not connector_id:
        return {"error": "missing_argument",
                "detail": "connector_id required (non-empty string)"}

    cfg = load_connectors_config()
    if cfg["errors"]:
        return {"error": "connector_config_invalid", "detail": cfg["errors"]}

    entry = next((c for c in cfg["connectors"] if c["id"] == connector_id), None)
    if entry is None:
        return {"error": "connector_not_found",
                "detail": f"no connector with id {connector_id!r} in {cfg['config_path']}",
                "available": [c["id"] for c in cfg["connectors"]]}

    record = await export_record({"id": memory_id})
    if "error" in record:
        return {"error": "memory_not_found", "detail": record["error"]}

    from connectors import get_connector
    try:
        connector = get_connector(entry["type"])
    except ValueError as e:
        return {"error": "unknown_connector_type", "detail": str(e)}

    try:
        envelope = connector.build_envelope_from_record(record, connector_id)
    except (TypeError, ValueError) as e:
        return {"error": "build_envelope_failed", "detail": str(e)}

    body = connector.format_envelope(envelope)
    body_size = len(body)
    # Slack §5.4: 40 KB body ceiling; 38 KB recommended push-side cap.
    # Other substrates can override via a future Connector.max_body_size
    # property (per audit's expanded ABC §"H4"); v0.5 hardcodes Slack's cap.
    SUBSTRATE_BODY_CAP = 38_000
    if body_size > SUBSTRATE_BODY_CAP:
        return {"error": "body_too_large_for_substrate",
                "detail": f"rendered body is {body_size} chars; "
                          f"substrate cap is {SUBSTRATE_BODY_CAP} chars",
                "size": body_size, "limit": SUBSTRATE_BODY_CAP}

    return {
        "body":         body,
        "channel":      entry.get("channel", ""),
        "connector_id": connector_id,
        "type":         entry["type"],
        "submitted_at": envelope.get("submitted_at", ""),
        "envelope":     envelope,
        "body_size":    body_size,
    }


async def ingest_connector_message(args: dict) -> dict:
    """Parse + ingest one substrate message body. Powers /remember pull.

    The symmetric primitive to `build_connector_envelope`. Combines:
      Connector.parse_envelope(body)
        → smoke-test filter (G11; default skip is_smoke_test=true)
        → core.store_memory_from_envelope(envelope, connector_type)

    Inputs (args):
      body                — the substrate message body (e.g., one Slack message)
      connector_id        — connector entry id in connector.json
      include_smoke_tests — optional bool; default false. When false, envelopes
                            with `is_smoke_test: true` are filtered before
                            storage and return {status: "smoke_test_skipped"}.
                            G11 receive-side filter — keeps the pre-launch
                            test message scaffolding (#mf-test-connector) out
                            of production memory stores by default.

    Returns one of:
      {"status": "stored",            "id": ..., "connector_ids": [...],
                                       "submitted_at": ...}
      {"status": "merged",            "id": ..., "connector_ids": [...],
                                       "submitted_at": ...}
      {"status": "duplicate",         "id": ..., "submitted_at": ...}
      {"status": "loopback_skipped",  "detail": ...,
                                       "submitted_at": ...}
      {"status": "smoke_test_skipped","detail": "is_smoke_test=true; skip"}
      {"status": "not_envelope",      "detail": "no fenced envelope found in body"}
      {"error":  "integrity_failed",  "detail": ...,            "submitted_at": ...?}
      {"error":  "missing_field",     "detail": ...}
      {"error":  "connector_config_invalid", "detail": [str]}
      {"error":  "connector_not_found",      "detail": str, "available": [str]}
      {"error":  "missing_argument",  "detail": str}

    The `submitted_at` field appears on outcomes where it could be parsed
    (everything that got past parse_envelope) — callers use it to advance
    the cursor at end-of-pull.
    """
    body         = args.get("body")
    connector_id = args.get("connector_id")
    include_smoke = bool(args.get("include_smoke_tests", False))

    if not isinstance(body, str):
        return {"error": "missing_argument",
                "detail": "body required (string)"}
    if not isinstance(connector_id, str) or not connector_id:
        return {"error": "missing_argument",
                "detail": "connector_id required (non-empty string)"}

    cfg = load_connectors_config()
    if cfg["errors"]:
        return {"error": "connector_config_invalid", "detail": cfg["errors"]}

    entry = next((c for c in cfg["connectors"] if c["id"] == connector_id), None)
    if entry is None:
        return {"error": "connector_not_found",
                "detail": f"no connector with id {connector_id!r} in {cfg['config_path']}",
                "available": [c["id"] for c in cfg["connectors"]]}

    connector_type = entry["type"]
    from connectors import get_connector
    try:
        connector = get_connector(connector_type)
    except ValueError as e:
        return {"error": "unknown_connector_type", "detail": str(e)}

    envelope = connector.parse_envelope(body)
    if envelope is None:
        return {"status": "not_envelope",
                "detail": "no fenced envelope found in body (likely a "
                          "human-authored message in this channel)"}

    # G11: receive-side smoke-test filter. Default skip; override with
    # include_smoke_tests=true if the caller is e2e-testing against this
    # exact channel.
    if envelope.get("is_smoke_test") is True and not include_smoke:
        return {"status": "smoke_test_skipped",
                "detail": "envelope is_smoke_test=true; skipped per G11 default",
                "submitted_at": envelope.get("submitted_at", "")}

    result = await store_memory_from_envelope(envelope, connector_type)
    # Surface submitted_at for cursor advancement on success outcomes.
    if "submitted_at" not in result:
        result["submitted_at"] = envelope.get("submitted_at", "")
    return result


async def get_connector_cursor_tool(args: dict) -> dict:
    """MCP tool wrapper for get_connector_cursor. Returns {cursor: str|null}."""
    connector_id = args.get("connector_id")
    if not isinstance(connector_id, str) or not connector_id:
        return {"error": "missing_argument", "detail": "connector_id required"}
    return {"connector_id": connector_id,
            "cursor": get_connector_cursor(connector_id)}


async def set_connector_cursor_tool(args: dict) -> dict:
    """MCP tool wrapper for set_connector_cursor."""
    connector_id = args.get("connector_id")
    iso_ts       = args.get("iso_ts")
    if not isinstance(connector_id, str) or not connector_id:
        return {"error": "missing_argument", "detail": "connector_id required"}
    if not isinstance(iso_ts, str) or not iso_ts:
        return {"error": "missing_argument", "detail": "iso_ts required (non-empty string)"}
    set_connector_cursor(connector_id, iso_ts)
    return {"status": "ok", "connector_id": connector_id, "cursor": iso_ts}


async def search_memory(args: dict) -> dict:
    """Semantic search via cosine similarity on the local collection.

    Optional `connector_id` filter: when set, returns only memories whose
    `connector_ids` list contains that value (Qdrant keyword-list match).
    """
    query          = args["query"]
    top_k          = min(int(args.get("top_k", 8)), 20)
    project        = args.get("project")
    type_          = args.get("type")
    since          = args.get("since")
    min_importance = int(args.get("min_importance", 1))
    connector_id   = args.get("connector_id")

    vec = await embed(query)
    if isinstance(vec, dict):
        return vec  # error from embed() — propagate verbatim

    filt = build_filter(project=project, type_=type_, since=since,
                        min_importance=min_importance, connector_id=connector_id)
    hits = qdrant.search(collection_name=COLLECTION, query_vector=vec,
                         limit=top_k, query_filter=filt, with_payload=True)
    results = format_results(hits)
    return {"query": query, "count": len(results), "results": results}


async def search_recent(args: dict) -> dict:
    """Time-filtered scroll — no vector search needed."""
    hours   = float(args.get("hours", 24))
    project = args.get("project")
    top_k   = min(int(args.get("top_k", 10)), 50)

    since_dt   = datetime.now(timezone.utc) - timedelta(hours=hours)
    conditions = [FieldCondition(key="timestamp", range=DatetimeRange(gte=since_dt))]
    if project:
        conditions.append(FieldCondition(key="project", match=MatchValue(value=project)))

    points, _ = qdrant.scroll(collection_name=COLLECTION,
                              scroll_filter=Filter(must=conditions),
                              limit=top_k, with_payload=True, with_vectors=False)
    results = [{
        "id": str(p.id), "content": p.payload.get("content", ""),
        "type": p.payload.get("type", ""), "project": p.payload.get("project", ""),
        "importance": p.payload.get("importance", 3),
        "timestamp": p.payload.get("timestamp", ""),
        "origin_node": p.payload.get("origin_node", ""),
        "received_at": p.payload.get("received_at", ""),
        "connector_ids": entry_connector_ids(p.payload or {}),
    } for p in sorted(points, key=lambda x: x.payload.get("timestamp", ""), reverse=True)]
    return {"hours": hours, "count": len(results), "results": results}


async def upsert_memory(args: dict) -> dict:
    """Update an existing point by id. Re-embeds the new content. Groups are
    preserved (use add_groups to widen sharing)."""
    memory_id  = args["id"]
    content    = args["content"]
    type_      = args.get("type")
    tags       = args.get("tags")
    importance = args.get("importance")

    vec = await embed(content)
    if isinstance(vec, dict):
        return vec  # error from embed() — propagate verbatim

    existing = qdrant.retrieve(collection_name=COLLECTION, ids=[memory_id], with_payload=True)
    if not existing:
        return {"error": f"Memory {memory_id} not found"}

    payload = dict(existing[0].payload)
    payload["content"]      = content
    payload["content_hash"] = content_hash(content)
    payload["timestamp"]    = iso_now()
    payload["groups"]       = entry_groups(payload)
    if type_:      payload["type"]       = type_
    if tags:       payload["tags"]       = tags
    if importance: payload["importance"] = importance

    qdrant.upsert(collection_name=COLLECTION,
                  points=[PointStruct(id=memory_id, vector=vec, payload=payload)])
    return {"status": "updated", "id": memory_id, "groups": payload["groups"]}


async def find_or_create(args: dict) -> dict:
    """Search for similar content first; store if no result above 0.82 similarity.
    If a near-duplicate is found, additively merges the caller's groups
    and connector_ids into it."""
    content       = args["content"]
    type_         = args["type"]
    tags          = args.get("tags", [])
    project       = args.get("project", "")
    importance    = int(args.get("importance", 3))
    groups        = normalize_groups_arg(args.get("groups"))
    connector_ids = normalize_connector_ids_arg(args.get("connector_ids"))

    vec = await embed(content)
    if isinstance(vec, dict):
        return vec  # error from embed() — propagate verbatim

    hits = qdrant.search(collection_name=COLLECTION, query_vector=vec, limit=1, with_payload=True)
    if hits and hits[0].score > 0.82:
        hit_id     = str(hits[0].id)
        current_g  = entry_groups(hits[0].payload or {})
        current_c  = entry_connector_ids(hits[0].payload or {})
        merged_g   = union_groups(current_g, groups)
        merged_c   = union_groups(current_c, connector_ids)
        payload_update = {}
        if merged_g != current_g:
            payload_update["groups"] = merged_g
        if merged_c != current_c:
            payload_update["connector_ids"] = merged_c
        if payload_update:
            qdrant.set_payload(collection_name=COLLECTION,
                               payload=payload_update, points=[hit_id])
        r = format_results([hits[0]])[0]
        r["groups"] = merged_g
        r["connector_ids"] = merged_c
        return {"status": "found", **r}

    chash    = content_hash(content)
    point_id = str(uuid.uuid4())
    ts       = iso_now()
    qdrant.upsert(collection_name=COLLECTION, points=[PointStruct(
        id=point_id, vector=vec,
        payload={
            "content": content, "type": type_, "tags": tags, "project": project,
            "importance": importance, "content_hash": chash,
            "timestamp": ts, "groups": groups, "connector_ids": connector_ids,
            "origin_node": _resolve_node_name(), "submitted_at": ts,
        },
    )])
    return {"status": "created", "id": point_id,
            "groups": groups, "connector_ids": connector_ids}


async def delete_memory(args: dict) -> dict:
    memory_id = args["id"]
    qdrant.delete(collection_name=COLLECTION, points_selector=[memory_id])
    return {"status": "deleted", "id": memory_id}


async def get_related(args: dict) -> dict:
    """Find memories semantically similar to a given memory id."""
    memory_id = args["memory_id"]
    top_k     = min(int(args.get("top_k", 5)), 20)

    existing = qdrant.retrieve(collection_name=COLLECTION, ids=[memory_id],
                               with_vectors=True, with_payload=True)
    if not existing:
        return {"error": f"Memory {memory_id} not found"}

    vec  = existing[0].vector
    filt = Filter(must_not=[FieldCondition(key="content_hash",
        match=MatchValue(value=existing[0].payload.get("content_hash", "")))])
    hits = qdrant.search(collection_name=COLLECTION, query_vector=vec,
                         limit=top_k + 1, query_filter=filt, with_payload=True)
    hits = [h for h in hits if str(h.id) != memory_id][:top_k]
    return {"reference_id": memory_id, "count": len(hits), "results": format_results(hits)}


async def memory_stats(args: dict) -> dict:
    info  = qdrant.get_collection(COLLECTION)
    total = info.points_count or 0

    types = ["decision", "fact", "preference", "error", "code", "context", "session"]
    by_type = {}
    for t in types:
        count_result = qdrant.count(
            collection_name=COLLECTION,
            count_filter=Filter(must=[FieldCondition(key="type", match=MatchValue(value=t))]),
            exact=False,
        )
        by_type[t] = count_result.count

    all_points, _ = qdrant.scroll(collection_name=COLLECTION, limit=10000,
                                  with_payload=["timestamp"], with_vectors=False)
    timestamps  = [p.payload.get("timestamp") for p in all_points if p.payload.get("timestamp")]
    last_stored = max(timestamps) if timestamps else "none"

    return {"total_memories": total, "by_type": by_type, "last_stored": last_stored,
            "collection": COLLECTION, "qdrant_url": QDRANT_URL, "ollama_url": OLLAMA_URL}


async def export_record(args: dict) -> dict:
    """Return the full Qdrant record (including vector) for a memory by id.

    Enables Constellation and other group-sharing clients to extract a
    complete record for faithful sending across peers — vector copied
    verbatim rather than re-embedded.
    """
    memory_id = args["id"]
    points = qdrant.retrieve(collection_name=COLLECTION, ids=[memory_id],
                             with_vectors=True, with_payload=True)
    if not points:
        return {"error": f"Memory {memory_id} not found"}
    p  = points[0]
    pl = p.payload or {}
    return {
        "id":            str(p.id),
        "vector":        p.vector,
        "content":       pl.get("content", ""),
        "content_hash":  pl.get("content_hash", ""),
        "type":          pl.get("type", ""),
        "tags":          pl.get("tags", []),
        "project":       pl.get("project", ""),
        "importance":    pl.get("importance", 3),
        "session_id":    pl.get("session_id", ""),
        "timestamp":     pl.get("timestamp", ""),
        "groups":        entry_groups(pl),
        "connector_ids": entry_connector_ids(pl),
        "origin_node":   pl.get("origin_node", _resolve_node_name()),
        "submitted_at":  pl.get("submitted_at", pl.get("timestamp", "")),
    }


# ── Group pull primitive ───────────────────────────────────────────────────
def get_entries_for_pull(group_name: str,
                         cursor_iso: str | None,
                         limit: int = 10000) -> list[dict]:
    """Return ALL entries where `group_name` ∈ entry.groups (cursor ignored).

    Used by constellation's GET /memory/since endpoint to answer pull queries
    from other peers. Returns full memory records (including vector) so the
    requesting peer can insert them locally without a follow-up fetch and
    without re-embedding.

    HISTORY: pre-0.5.0-013, this function filtered by `submitted_at > cursor`
    to limit response size to "new" entries. That cursor model was incorrect:
    `submitted_at` is the originating peer's timestamp, but a back-fill
    scenario where peer Y's older memory arrives at relay X after our last
    pull would have `submitted_at` BELOW our cursor — and the filter would
    silently exclude it. The bug manifested as tk421 missing 75 memories
    despite "successful" pulls. Fix shape: drop the cursor filter; rely on
    receiver-side content_hash dedup (already in place in /memory/put and
    in Constellation's _merge_pulled). Cost: each pull is now O(group_size)
    bytes on the wire. At current scale (<10K entries per group) this is
    sub-100ms transfer + parse; revisit if Constellation scales past 50K
    entries per group, at which point per-origin cursor or a bloom-filter
    "what I already have" exchange becomes worthwhile.

    The `cursor_iso` parameter is preserved for wire-format back-compat with
    older clients that still send it; it is ignored server-side.

    Each /memory/since call answers for a single group; the wire-shape's
    `groups` field carries only the requested group. Multi-group entries
    get reconstructed on the caller's side via additive dedup-merge when
    the caller pulls other groups.
    """
    # cursor_iso intentionally unused — see docstring "HISTORY" note.
    _ = cursor_iso  # explicit silencer for linters

    conditions = [
        FieldCondition(key="groups", match=MatchValue(value=group_name)),
    ]

    points, _ = qdrant.scroll(
        collection_name=COLLECTION,
        scroll_filter=Filter(must=conditions),
        limit=limit, with_payload=True, with_vectors=True,
    )
    points = sorted(points, key=lambda p: (p.payload or {}).get("submitted_at", ""))
    return [{
        "content":      (p.payload or {}).get("content", ""),
        "content_hash": (p.payload or {}).get("content_hash", ""),
        "vector":       list(p.vector) if p.vector is not None else None,
        "type":         (p.payload or {}).get("type", ""),
        "tags":         (p.payload or {}).get("tags", []),
        "project":      (p.payload or {}).get("project", ""),
        "importance":   (p.payload or {}).get("importance", 3),
        "groups":       [group_name],
        "origin_node":  (p.payload or {}).get("origin_node", ""),
        "submitted_at": (p.payload or {}).get("submitted_at", ""),
    } for p in points]


def max_submitted_at_in_group(group_name: str) -> str | None:
    """Return the highest submitted_at currently stored locally for the given
    group (where the group appears in entry.groups), or None if no entries.

    Used by /pull on the local gateway to derive the cursor at sync time
    (no stored cursor state — derived from local Qdrant on every pull).
    """
    points, _ = qdrant.scroll(
        collection_name=COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="groups", match=MatchValue(value=group_name)),
        ]),
        limit=10000, with_payload=["submitted_at"], with_vectors=False,
    )
    stamps = [(p.payload or {}).get("submitted_at") for p in points
              if (p.payload or {}).get("submitted_at")]
    return max(stamps) if stamps else None


# ── Legacy v0.3 → v0.4 migration (idempotent, opt-in at daemon startup) ───
def migrate_legacy_entries(log_=None) -> dict:
    """Scan the collection for v0.3 entries missing a `groups` payload and
    backfill it from {source, group_name}. Idempotent — entries already
    carrying `groups` are left alone. Runs in one pass; safe to call on
    every daemon startup (no-op once everything is migrated).

    Returns: {scanned, migrated, skipped}.
    """
    log_ = log_ or log
    scanned = migrated = skipped = 0
    offset  = None
    while True:
        try:
            points, offset = qdrant.scroll(
                collection_name=COLLECTION,
                limit=512, offset=offset,
                with_payload=True, with_vectors=False,
            )
        except Exception as e:
            log_.warning("legacy-migration scroll failed: %s", e)
            break
        for p in points:
            scanned += 1
            pl = p.payload or {}
            if isinstance(pl.get("groups"), list) and pl.get("groups"):
                skipped += 1
                continue
            groups = entry_groups(pl)
            try:
                qdrant.set_payload(collection_name=COLLECTION,
                                   payload={"groups": groups}, points=[p.id])
                migrated += 1
            except Exception as e:
                log_.warning("legacy-migration failed for %s: %s", p.id, e)
        if offset is None:
            break
    if migrated:
        log_.info("legacy-migration: migrated=%d skipped=%d scanned=%d",
                  migrated, skipped, scanned)
    return {"scanned": scanned, "migrated": migrated, "skipped": skipped}
