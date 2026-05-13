#!/usr/bin/env python3
"""
core.py — the shared memory layer over Qdrant.

Both mem_fusion.py (stdio MCP server, serving Claude) and constellation.py
(HTTP server, serving peers) are thin proxies on top of these functions.
This is the only module that talks to Qdrant.

Functions return plain dicts; transport-specific serialization (MCP JSON,
HTTP JSON, etc.) is the caller's responsibility.

If Ollama is unreachable when embedding is required, returns
{"error": "ollama_unreachable", "detail": ...}. Callers surface the error.
No silent queueing.
"""
import hashlib
import logging
import os
import uuid
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
# The collection name is invariant. Mem-fusion fuses cowork-memory entries
# from ephemeral file storage into persistent vector storage; the collection
# *is* the cowork-memory store. Group-shared, local, preload distinctions
# live in the `source` payload field, never in the collection name.
COLLECTION  = "cowork_memories"
EMBED_MODEL = "nomic-embed-text"
VECTOR_SIZE = 768

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
async def embed(text: str) -> list[float] | None:
    """Generate a 768-dim vector via local Ollama. Returns None on failure."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/embeddings",
                                  json={"model": EMBED_MODEL, "prompt": text})
            r.raise_for_status()
            return r.json()["embedding"]
    except Exception as e:
        log.error("embed failed: %s", e)
        return None


# ── Qdrant filter helpers ─────────────────────────────────────────────────
def find_duplicate(chash: str) -> str | None:
    """Return existing point id if a memory with this content_hash exists, else None."""
    try:
        results, _ = qdrant.scroll(
            collection_name=COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="content_hash", match=MatchValue(value=chash))
            ]),
            limit=1, with_payload=False,
        )
        if results:
            return str(results[0].id)
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
    } for h in hits]


# ── Memory operations ─────────────────────────────────────────────────────
async def store_memory(args: dict) -> dict:
    """Embed, dedup, insert. Returns one of:
       {status: "stored", id}            — fresh content stored
       {status: "duplicate", existing_id} — content_hash already present
       {error: "ollama_unreachable"}     — embed failed; caller surfaces error
    """
    content    = args["content"]
    type_      = args["type"]
    tags       = args.get("tags", [])
    project    = args.get("project", "")
    importance = int(args.get("importance", 3))
    session_id = args.get("session_id", "")

    chash   = content_hash(content)
    dupe_id = find_duplicate(chash)
    if dupe_id:
        return {"status": "duplicate", "existing_id": dupe_id}

    vec = await embed(content)
    if vec is None:
        return {"error": "ollama_unreachable",
                "detail": "Local Ollama did not respond; cannot embed. Verify Ollama is running."}

    point_id = str(uuid.uuid4())
    qdrant.upsert(collection_name=COLLECTION, points=[PointStruct(
        id=point_id, vector=vec,
        payload={
            "content": content, "type": type_, "tags": tags, "project": project,
            "importance": importance, "session_id": session_id,
            "content_hash": chash, "timestamp": iso_now(), "source": "local",
        },
    )])
    return {"status": "stored", "id": point_id}


async def search_memory(args: dict) -> dict:
    """Semantic search via cosine similarity on the local collection."""
    query          = args["query"]
    top_k          = min(int(args.get("top_k", 8)), 20)
    project        = args.get("project")
    type_          = args.get("type")
    since          = args.get("since")
    min_importance = int(args.get("min_importance", 1))

    vec = await embed(query)
    if vec is None:
        return {"error": "ollama_unreachable",
                "detail": "Local Ollama did not respond; cannot perform semantic search."}

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
    """Update an existing point by id. Re-embeds the new content."""
    memory_id  = args["id"]
    content    = args["content"]
    type_      = args.get("type")
    tags       = args.get("tags")
    importance = args.get("importance")

    vec = await embed(content)
    if vec is None:
        return {"error": "ollama_unreachable",
                "detail": "Local Ollama did not respond; cannot re-embed for upsert."}

    existing = qdrant.retrieve(collection_name=COLLECTION, ids=[memory_id], with_payload=True)
    if not existing:
        return {"error": f"Memory {memory_id} not found"}

    payload = dict(existing[0].payload)
    payload["content"]      = content
    payload["content_hash"] = content_hash(content)
    payload["timestamp"]    = iso_now()
    if type_:      payload["type"]       = type_
    if tags:       payload["tags"]       = tags
    if importance: payload["importance"] = importance

    qdrant.upsert(collection_name=COLLECTION,
                  points=[PointStruct(id=memory_id, vector=vec, payload=payload)])
    return {"status": "updated", "id": memory_id}


async def find_or_create(args: dict) -> dict:
    """Search for similar content first; store if no result above 0.82 similarity."""
    content    = args["content"]
    type_      = args["type"]
    tags       = args.get("tags", [])
    project    = args.get("project", "")
    importance = int(args.get("importance", 3))

    vec = await embed(content)
    if vec is None:
        return {"error": "ollama_unreachable",
                "detail": "Local Ollama did not respond; cannot embed for find_or_create."}

    hits = qdrant.search(collection_name=COLLECTION, query_vector=vec, limit=1, with_payload=True)
    if hits and hits[0].score > 0.82:
        r = format_results([hits[0]])[0]
        return {"status": "found", **r}

    chash    = content_hash(content)
    point_id = str(uuid.uuid4())
    qdrant.upsert(collection_name=COLLECTION, points=[PointStruct(
        id=point_id, vector=vec,
        payload={
            "content": content, "type": type_, "tags": tags, "project": project,
            "importance": importance, "content_hash": chash,
            "timestamp": iso_now(), "source": "local",
        },
    )])
    return {"status": "created", "id": point_id}


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
    p = points[0]
    return {
        "id":           str(p.id),
        "vector":       p.vector,
        "content":      p.payload.get("content", ""),
        "content_hash": p.payload.get("content_hash", ""),
        "type":         p.payload.get("type", ""),
        "tags":         p.payload.get("tags", []),
        "project":      p.payload.get("project", ""),
        "importance":   p.payload.get("importance", 3),
        "session_id":   p.payload.get("session_id", ""),
        "timestamp":    p.payload.get("timestamp", ""),
        "source":       p.payload.get("source", ""),
    }


# ── Group pull primitive ───────────────────────────────────────────────────
def get_entries_for_pull(group_name: str,
                         cursor_iso: str | None,
                         limit: int = 256) -> list[dict]:
    """Return group entries with submitted_at > cursor_iso, sorted ascending.

    Used by constellation's GET /memory/since endpoint to answer pull queries
    from other peers. Filters on submitted_at — the originating peer's
    timestamp, which is global across the group — so cursors are comparable
    no matter which peer answers the query.

    Returns full memory records (including vector) so the requesting peer can
    insert them locally without a follow-up fetch and without re-embedding.
    """
    conditions = [
        FieldCondition(key="group_name", match=MatchValue(value=group_name)),
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
    points = sorted(points, key=lambda p: p.payload.get("submitted_at", ""))
    return [{
        "content":      p.payload.get("content", ""),
        "content_hash": p.payload.get("content_hash", ""),
        "vector":       list(p.vector) if p.vector is not None else None,
        "type":         p.payload.get("type", ""),
        "tags":         p.payload.get("tags", []),
        "project":      p.payload.get("project", ""),
        "importance":   p.payload.get("importance", 3),
        "group_name":   p.payload.get("group_name", ""),
        "origin_node":  p.payload.get("origin_node", ""),
        "submitted_at": p.payload.get("submitted_at", ""),
    } for p in points]


def max_submitted_at_in_group(group_name: str) -> str | None:
    """Return the highest submitted_at currently stored locally for a group,
    or None if no entries exist for that group yet.

    Used by /pull on the local gateway to derive the cursor at sync time
    (no stored cursor state — derived from local Qdrant on every pull).
    """
    points, _ = qdrant.scroll(
        collection_name=COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="group_name", match=MatchValue(value=group_name)),
        ]),
        limit=10000, with_payload=["submitted_at"], with_vectors=False,
    )
    stamps = [p.payload.get("submitted_at") for p in points if p.payload.get("submitted_at")]
    return max(stamps) if stamps else None
