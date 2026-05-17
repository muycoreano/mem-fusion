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
    """The dedup + integrity hash. Must be byte-identical across all daemons."""
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


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


def build_filter(project=None, type_=None, since=None, min_importance=1):
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
    return Filter(must=conditions) if conditions else None


def format_results(hits):
    """Shape Qdrant hits into the wire-friendly result format."""
    return [{
        "id":         str(h.id),
        "score":      round(h.score, 4),
        "content":    h.payload.get("content", ""),
        "type":       h.payload.get("type", ""),
        "project":    h.payload.get("project", ""),
        "tags":       h.payload.get("tags", []),
        "importance": h.payload.get("importance", 3),
        "timestamp":  h.payload.get("timestamp", ""),
        "groups":     entry_groups(h.payload or {}),
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
    content    = args["content"]
    type_      = args["type"]
    tags       = args.get("tags", [])
    project    = args.get("project", "")
    importance = int(args.get("importance", 3))
    session_id = args.get("session_id", "")
    groups     = normalize_groups_arg(args.get("groups"))

    chash    = content_hash(content)
    existing = find_existing_by_hash(chash)
    if existing:
        existing_id, payload = existing
        current = entry_groups(payload)
        merged  = union_groups(current, groups)
        if merged == current:
            return {"status": "duplicate", "id": existing_id, "groups": current}
        qdrant.set_payload(collection_name=COLLECTION,
                           payload={"groups": merged}, points=[existing_id])
        return {"status": "merged", "id": existing_id, "groups": merged}

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
            "groups": groups, "origin_node": NODE_NAME, "submitted_at": ts,
        },
    )])
    return {"status": "stored", "id": point_id, "groups": groups}


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


async def search_memory(args: dict) -> dict:
    """Semantic search via cosine similarity on the local collection."""
    query          = args["query"]
    top_k          = min(int(args.get("top_k", 8)), 20)
    project        = args.get("project")
    type_          = args.get("type")
    since          = args.get("since")
    min_importance = int(args.get("min_importance", 1))

    vec = await embed(query)
    if isinstance(vec, dict):
        return vec  # error from embed() — propagate verbatim

    filt = build_filter(project=project, type_=type_, since=since, min_importance=min_importance)
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
    If a near-duplicate is found, additively merges the caller's groups into it."""
    content    = args["content"]
    type_      = args["type"]
    tags       = args.get("tags", [])
    project    = args.get("project", "")
    importance = int(args.get("importance", 3))
    groups     = normalize_groups_arg(args.get("groups"))

    vec = await embed(content)
    if isinstance(vec, dict):
        return vec  # error from embed() — propagate verbatim

    hits = qdrant.search(collection_name=COLLECTION, query_vector=vec, limit=1, with_payload=True)
    if hits and hits[0].score > 0.82:
        hit_id  = str(hits[0].id)
        current = entry_groups(hits[0].payload or {})
        merged  = union_groups(current, groups)
        if merged != current:
            qdrant.set_payload(collection_name=COLLECTION,
                               payload={"groups": merged}, points=[hit_id])
        r = format_results([hits[0]])[0]
        r["groups"] = merged
        return {"status": "found", **r}

    chash    = content_hash(content)
    point_id = str(uuid.uuid4())
    ts       = iso_now()
    qdrant.upsert(collection_name=COLLECTION, points=[PointStruct(
        id=point_id, vector=vec,
        payload={
            "content": content, "type": type_, "tags": tags, "project": project,
            "importance": importance, "content_hash": chash,
            "timestamp": ts, "groups": groups,
            "origin_node": NODE_NAME, "submitted_at": ts,
        },
    )])
    return {"status": "created", "id": point_id, "groups": groups}


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
        "id":           str(p.id),
        "vector":       p.vector,
        "content":      pl.get("content", ""),
        "content_hash": pl.get("content_hash", ""),
        "type":         pl.get("type", ""),
        "tags":         pl.get("tags", []),
        "project":      pl.get("project", ""),
        "importance":   pl.get("importance", 3),
        "session_id":   pl.get("session_id", ""),
        "timestamp":    pl.get("timestamp", ""),
        "groups":       entry_groups(pl),
        "origin_node":  pl.get("origin_node", NODE_NAME),
        "submitted_at": pl.get("submitted_at", pl.get("timestamp", "")),
    }


# ── Group pull primitive ───────────────────────────────────────────────────
def get_entries_for_pull(group_name: str,
                         cursor_iso: str | None,
                         limit: int = 256) -> list[dict]:
    """Return entries where `group_name` ∈ entry.groups and submitted_at > cursor.

    Used by constellation's GET /memory/since endpoint to answer pull queries
    from other peers. Filters on submitted_at — the originating peer's
    timestamp, which is global across the group — so cursors are comparable
    no matter which peer answers the query.

    Each /memory/since call answers for a single group; the wire-shape's
    `groups` field carries only the requested group. Multi-group entries
    get reconstructed on the caller's side via additive dedup-merge when
    the caller pulls other groups.

    Returns full memory records (including vector) so the requesting peer can
    insert them locally without a follow-up fetch and without re-embedding.
    """
    conditions = [
        FieldCondition(key="groups", match=MatchValue(value=group_name)),
    ]
    if cursor_iso:
        conditions.append(FieldCondition(
            key="submitted_at",
            range=DatetimeRange(gt=datetime.fromisoformat(cursor_iso)),
        ))

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
