# Mem-Fusion — Setup Prompt for Claude Code

> **How to use this file:** Open Claude Code in a terminal on your Mac and paste this entire document as your first message. Claude will read it, run the steps, and verify everything works. You should not need to do anything except answer prompts and approve commands.

---

## What this builds

A local persistent vector memory system that gives your Claude Code CLI long-term, semantically-searchable memory across sessions. After install you'll be able to:

- Have Claude **store** decisions, errors, preferences, and context with `store_memory(...)`
- Have Claude **recall** them later with `search_memory("...")` — even weeks later, in a brand-new session
- Get **automatic context priming** — every prompt is searched against your memory store and relevant hits are injected silently
- Use `/remember <thing>` as a one-liner to pin something important

It's all local — no data leaves your Mac. Stack:

- **Qdrant 1.13.4** — local vector database (port 6333)
- **Ollama 0.20.5** + `nomic-embed-text` — local 768-dim embeddings (port 11434)
- **Python 3.12 MCP server** — exposes 8 memory tools to Claude Code via stdio
- **4 hooks** — SessionStart, UserPromptSubmit, Stop, PostToolUse:Write
- **`/remember` skill**

Everything is managed by `launchd`. Disk footprint ≈ 200 MB.

---

## Pre-flight — verify before starting

Run these checks first. If any fail, pause and tell the user before proceeding.

```bash
# 1. macOS only (this script is Mac-specific)
[[ "$(uname)" == "Darwin" ]] || { echo "This installer is macOS-only"; exit 1; }

# 2. Architecture detection
ARCH="$(uname -m)"   # arm64 (Apple Silicon) or x86_64 (Intel)
echo "Arch: $ARCH"

# 3. Homebrew present + correct prefix for this arch
if [[ "$ARCH" == "arm64" ]]; then
  BREW_PREFIX="/opt/homebrew"
else
  BREW_PREFIX="/usr/local"
fi
[[ -x "$BREW_PREFIX/bin/brew" ]] || { echo "Install Homebrew first: https://brew.sh"; exit 1; }

# 4. Claude CLI present
command -v claude >/dev/null || { echo "Install Claude Code first"; exit 1; }

# 5. Nothing already on the mem-fusion ports
lsof -nP -iTCP:6333 -sTCP:LISTEN -t >/dev/null 2>&1 && echo "WARN: port 6333 already in use"
lsof -nP -iTCP:11434 -sTCP:LISTEN -t >/dev/null 2>&1 && echo "INFO: port 11434 (Ollama) already in use — will reuse"

# 6. No prior mem-fusion MCP registered
claude mcp list 2>/dev/null | grep -q "mem-fusion" && echo "WARN: mem-fusion MCP already registered — will overwrite"

# 7. Backup any existing settings.json before we touch it
[[ -f ~/.claude/settings.json ]] && cp ~/.claude/settings.json ~/.claude/settings.json.bak.$(date +%Y%m%d-%H%M%S)
```

If port 6333 is taken or another `mem-fusion` MCP is already registered, **stop and ask the user** before continuing.

---

## Step 1 — Install Homebrew dependencies

```bash
brew install python@3.12 ollama
ollama --version    # confirm 0.20+
python3.12 --version
```

If `python@3.12` is unavailable on the user's brew, fall back to `python@3.11` and adjust the venv command in Step 4 accordingly. The MCP server is compatible with 3.11+.

---

## Step 2 — Lay out the install directory

```bash
mkdir -p ~/.local/share/mem-fusion/{bin,scripts,logs,queue,qdrant-data,snapshots}
cd ~/.local/share/mem-fusion
```

---

## Step 3 — Download the Qdrant binary

Pin to **v1.13.4**. Pick the asset matching the user's arch.

```bash
QDRANT_VER="1.13.4"
if [[ "$(uname -m)" == "arm64" ]]; then
  ASSET="qdrant-aarch64-apple-darwin.tar.gz"
else
  ASSET="qdrant-x86_64-apple-darwin.tar.gz"
fi
curl -L -o /tmp/qdrant.tar.gz \
  "https://github.com/qdrant/qdrant/releases/download/v${QDRANT_VER}/${ASSET}"
tar -xzf /tmp/qdrant.tar.gz -C /tmp
mv /tmp/qdrant ~/.local/share/mem-fusion/bin/qdrant
chmod +x ~/.local/share/mem-fusion/bin/qdrant
~/.local/share/mem-fusion/bin/qdrant --version   # sanity check
```

If the asset name has changed in newer releases, check `https://github.com/qdrant/qdrant/releases/tag/v1.13.4` and adjust.

---

## Step 4 — Python venv + dependencies

```bash
python3.12 -m venv ~/.local/share/mem-fusion/venv
~/.local/share/mem-fusion/venv/bin/pip install --upgrade pip
~/.local/share/mem-fusion/venv/bin/pip install \
  mcp==1.6.0 qdrant-client==1.13.1 httpx==0.28.1
```

Save the pinned versions:

```bash
cat > ~/.local/share/mem-fusion/requirements.txt <<'EOF'
mcp==1.6.0
qdrant-client==1.13.1
httpx==0.28.1
EOF
```

---

## Step 5 — Write `qdrant-config.yaml`

```bash
cat > ~/.local/share/mem-fusion/qdrant-config.yaml <<EOF
storage:
  storage_path: $HOME/.local/share/mem-fusion/qdrant-data

snapshots_config:
  snapshots_path: $HOME/.local/share/mem-fusion/qdrant-data/snapshots

service:
  host: 127.0.0.1
  http_port: 6333
  grpc_port: 6334
  enable_cors: false

log_level: WARN
EOF
```

(Note: `<<EOF` without quotes around the delimiter — `$HOME` *will* expand. That's intentional.)

---

## Step 6 — Write the MCP server

Save the following to `~/.local/share/mem-fusion/mcp_server.py` **verbatim**:

```python
#!/usr/bin/env python3
"""
Mem-Fusion MCP Server — stdio transport
Provides 8 tools for storing and retrieving memories from Qdrant.
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from qdrant_client import QdrantClient
from qdrant_client.models import (
    DatetimeRange, Distance, FieldCondition, Filter, MatchValue,
    PointStruct, Range, VectorParams,
)

QDRANT_URL   = os.getenv("QDRANT_URL",   "http://127.0.0.1:6333")
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://127.0.0.1:11434")
COLLECTION   = os.getenv("MEMFUSION_COLLECTION", "mem_fusion_memories")
EMBED_MODEL  = "nomic-embed-text"
VECTOR_SIZE  = 768
LOG_PATH     = os.getenv("MEMFUSION_LOG",
                          str(Path.home() / ".local/share/mem-fusion/logs/mcp.log"))

QUEUE_DIR    = Path.home() / ".local/share/mem-fusion/queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mem-fusion")

qdrant = QdrantClient(url=QDRANT_URL, timeout=10)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


async def embed(text: str) -> list[float] | None:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/embeddings",
                                   json={"model": EMBED_MODEL, "prompt": text})
            r.raise_for_status()
            return r.json()["embedding"]
    except Exception as e:
        log.error("embed failed: %s", e)
        return None


def queue_write(payload: dict) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    h  = content_hash(payload.get("content", ""))
    fname = QUEUE_DIR / f"{ts}-{h}.json"
    fname.write_text(json.dumps(payload))
    log.warning("Queued write to %s (Ollama unavailable)", fname.name)
    return str(fname)


def find_duplicate(chash: str) -> str | None:
    try:
        results, _ = qdrant.scroll(
            collection_name=COLLECTION,
            scroll_filter=Filter(must=[FieldCondition(key="content_hash", match=MatchValue(value=chash))]),
            limit=1, with_payload=False,
        )
        if results:
            return str(results[0].id)
    except Exception as e:
        log.warning("Duplicate check failed: %s", e)
    return None


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def since_to_filter(since: str | None):
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


server = Server("mem-fusion")


@server.list_tools()
async def list_tools():
    return [
        Tool(name="store_memory",
             description=("Store a new memory in the persistent vector database. "
                          "Call after any decision, discovery, user preference, error resolution, "
                          "or substantial code written. Do NOT store trivial facts or transient state. "
                          "type one of: decision, fact, preference, error, code, context, session. "
                          "importance: 1=trivial, 3=normal, 4=important, 5=critical (user-curated only)."),
             inputSchema={"type": "object", "properties": {
                 "content":    {"type": "string"},
                 "type":       {"type": "string", "enum": ["decision","fact","preference","error","code","context","session"]},
                 "tags":       {"type": "array", "items": {"type": "string"}},
                 "project":    {"type": "string"},
                 "importance": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
                 "session_id": {"type": "string"},
             }, "required": ["content", "type"]}),
        Tool(name="search_memory",
             description=("Semantic search across all stored memories. Call at session start with the current task, "
                          "before architectural decisions, and on recurring errors. Scores >0.75 are meaningful."),
             inputSchema={"type": "object", "properties": {
                 "query":          {"type": "string"},
                 "top_k":          {"type": "integer", "default": 8},
                 "project":        {"type": "string"},
                 "type":           {"type": "string", "enum": ["decision","fact","preference","error","code","context","session"]},
                 "since":          {"type": "string", "description": "ISO date or relative '24h'/'7d'"},
                 "min_importance": {"type": "integer", "default": 1},
             }, "required": ["query"]}),
        Tool(name="search_recent",
             description="Fast time-filtered retrieval — recent memories without vector search.",
             inputSchema={"type": "object", "properties": {
                 "hours":   {"type": "number", "default": 24},
                 "project": {"type": "string"},
                 "top_k":   {"type": "integer", "default": 10},
             }, "required": []}),
        Tool(name="upsert_memory",
             description="Update an existing memory by ID.",
             inputSchema={"type": "object", "properties": {
                 "id":         {"type": "string"},
                 "content":    {"type": "string"},
                 "type":       {"type": "string", "enum": ["decision","fact","preference","error","code","context","session"]},
                 "tags":       {"type": "array", "items": {"type": "string"}},
                 "importance": {"type": "integer", "minimum": 1, "maximum": 5},
             }, "required": ["id", "content"]}),
        Tool(name="find_or_create",
             description="Search first, store if no result above 0.82.",
             inputSchema={"type": "object", "properties": {
                 "content":    {"type": "string"},
                 "type":       {"type": "string", "enum": ["decision","fact","preference","error","code","context","session"]},
                 "tags":       {"type": "array", "items": {"type": "string"}},
                 "project":    {"type": "string"},
                 "importance": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
             }, "required": ["content", "type"]}),
        Tool(name="delete_memory",
             description="Delete a memory by ID.",
             inputSchema={"type": "object", "properties": {
                 "id": {"type": "string"},
             }, "required": ["id"]}),
        Tool(name="get_related",
             description="Find memories similar to a given memory ID.",
             inputSchema={"type": "object", "properties": {
                 "memory_id": {"type": "string"},
                 "top_k":     {"type": "integer", "default": 5},
             }, "required": ["memory_id"]}),
        Tool(name="memory_stats",
             description="Total count, breakdown by type/project, last update timestamp.",
             inputSchema={"type": "object", "properties": {}, "required": []}),
    ]


@server.call_tool()
async def call_tool(name, arguments):
    try:
        result = await dispatch(name, arguments)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as e:
        log.exception("Tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def dispatch(name, args):
    if name == "store_memory":      return await tool_store(args)
    if name == "search_memory":     return await tool_search(args)
    if name == "search_recent":     return await tool_search_recent(args)
    if name == "upsert_memory":     return await tool_upsert(args)
    if name == "find_or_create":    return await tool_find_or_create(args)
    if name == "delete_memory":     return await tool_delete(args)
    if name == "get_related":       return await tool_get_related(args)
    if name == "memory_stats":      return await tool_stats(args)
    raise ValueError(f"Unknown tool: {name}")


async def tool_store(args):
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
        queued = queue_write({"content": content, "type": type_, "tags": tags,
                              "project": project, "importance": importance})
        return {"status": "queued", "queue_file": queued,
                "warning": "Ollama unavailable — memory queued for later ingestion"}

    point_id = str(uuid.uuid4())
    qdrant.upsert(collection_name=COLLECTION, points=[PointStruct(
        id=point_id, vector=vec,
        payload={"content": content, "type": type_, "tags": tags, "project": project,
                 "importance": importance, "session_id": session_id,
                 "content_hash": chash, "timestamp": iso_now(), "source": "manual"},
    )])
    return {"status": "stored", "id": point_id}


async def tool_search(args):
    query          = args["query"]
    top_k          = min(int(args.get("top_k", 8)), 20)
    project        = args.get("project")
    type_          = args.get("type")
    since          = args.get("since")
    min_importance = int(args.get("min_importance", 1))

    vec = await embed(query)
    if vec is None:
        return {"error": "Ollama unavailable — cannot perform semantic search"}

    filt = build_filter(project=project, type_=type_, since=since, min_importance=min_importance)
    hits = qdrant.search(collection_name=COLLECTION, query_vector=vec,
                         limit=top_k, query_filter=filt, with_payload=True)
    results = format_results(hits)
    return {"query": query, "count": len(results), "results": results}


async def tool_search_recent(args):
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


async def tool_upsert(args):
    memory_id  = args["id"]
    content    = args["content"]
    type_      = args.get("type")
    tags       = args.get("tags")
    importance = args.get("importance")

    vec = await embed(content)
    if vec is None:
        return {"error": "Ollama unavailable — cannot re-embed for upsert"}

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


async def tool_find_or_create(args):
    content    = args["content"]
    type_      = args["type"]
    tags       = args.get("tags", [])
    project    = args.get("project", "")
    importance = int(args.get("importance", 3))

    vec = await embed(content)
    if vec is None:
        queued = queue_write(args)
        return {"status": "queued", "queue_file": queued}

    hits = qdrant.search(collection_name=COLLECTION, query_vector=vec, limit=1, with_payload=True)
    if hits and hits[0].score > 0.82:
        r = format_results([hits[0]])[0]
        return {"status": "found", **r}

    chash    = content_hash(content)
    point_id = str(uuid.uuid4())
    qdrant.upsert(collection_name=COLLECTION, points=[PointStruct(
        id=point_id, vector=vec,
        payload={"content": content, "type": type_, "tags": tags, "project": project,
                 "importance": importance, "content_hash": chash,
                 "timestamp": iso_now(), "source": "manual"},
    )])
    return {"status": "created", "id": point_id}


async def tool_delete(args):
    memory_id = args["id"]
    qdrant.delete(collection_name=COLLECTION, points_selector=[memory_id])
    return {"status": "deleted", "id": memory_id}


async def tool_get_related(args):
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


async def tool_stats(args):
    info  = qdrant.get_collection(COLLECTION)
    total = info.points_count or 0

    types = ["decision", "fact", "preference", "error", "code", "context", "session"]
    by_type = {}
    for t in types:
        count_result = qdrant.count(collection_name=COLLECTION,
            count_filter=Filter(must=[FieldCondition(key="type", match=MatchValue(value=t))]),
            exact=False)
        by_type[t] = count_result.count

    all_points, _ = qdrant.scroll(collection_name=COLLECTION, limit=10000,
                                   with_payload=["timestamp"], with_vectors=False)
    timestamps  = [p.payload.get("timestamp") for p in all_points if p.payload.get("timestamp")]
    last_stored = max(timestamps) if timestamps else "none"

    return {"total_memories": total, "by_type": by_type, "last_stored": last_stored,
            "collection": COLLECTION, "qdrant_url": QDRANT_URL, "ollama_url": OLLAMA_URL}


async def main():
    log.info("Mem-Fusion MCP Server starting")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Step 7 — Write the collection initializer

Save to `~/.local/share/mem-fusion/scripts/init_collection.py`:

```python
#!/usr/bin/env python3
"""One-time setup: create the mem_fusion_memories collection with payload indexes."""
import os
import sys
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PayloadSchemaType

QDRANT_URL  = "http://127.0.0.1:6333"
COLLECTION  = os.getenv("MEMFUSION_COLLECTION", "mem_fusion_memories")
VECTOR_SIZE = 768

client   = QdrantClient(url=QDRANT_URL, timeout=10)
existing = [c.name for c in client.get_collections().collections]

if COLLECTION in existing:
    print(f"Collection '{COLLECTION}' already exists — skipping creation.")
else:
    client.create_collection(collection_name=COLLECTION,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE))
    print(f"Created collection '{COLLECTION}'.")

indexes = {
    "type":         PayloadSchemaType.KEYWORD,
    "project":      PayloadSchemaType.KEYWORD,
    "source":       PayloadSchemaType.KEYWORD,
    "session_id":   PayloadSchemaType.KEYWORD,
    "content_hash": PayloadSchemaType.KEYWORD,
    "importance":   PayloadSchemaType.INTEGER,
    "timestamp":    PayloadSchemaType.DATETIME,
}
for field, schema in indexes.items():
    try:
        client.create_payload_index(COLLECTION, field, schema)
        print(f"  Index: {field} ({schema.value})")
    except Exception as e:
        if "already exists" in str(e).lower():
            print(f"  Index: {field} (already exists)")
        else:
            print(f"  Index: {field} FAILED — {e}", file=sys.stderr)

info = client.get_collection(COLLECTION)
print(f"\nCollection ready. Vectors: {info.vectors_count or 0}")
```

---

## Step 8 — Write the 4 hook scripts

All four go in `~/.local/share/mem-fusion/scripts/`. Make each executable with `chmod +x`.

### 8a. `session_prime.sh` — SessionStart hook

```bash
#!/usr/bin/env bash
# SessionStart hook — emits a brief recent-memory snapshot for context priming.
VENV="$HOME/.local/share/mem-fusion/venv/bin/python"

$VENV - <<PYEOF
import sys, asyncio
sys.path.insert(0, "$HOME/.local/share/mem-fusion")
import mcp_server as srv

async def main():
    recent = await srv.tool_search_recent({"hours": 48, "top_k": 5})
    stats  = await srv.tool_stats({})
    total  = stats.get("total_memories", 0)
    last   = stats.get("last_stored", "none")

    lines = [
        "<memfusion_status>",
        f"  Vector memory: {total} memories stored | Last update: {last}",
    ]
    if recent.get("count", 0) > 0:
        lines.append("  Recent activity (last 48h):")
        for r in recent["results"][:3]:
            ts    = r.get("timestamp", "")[:10]
            ctype = r.get("type", "")
            proj  = r.get("project", "")
            snip  = r.get("content", "")[:80].replace("\n", " ")
            lines.append(f"    [{ts}] [{ctype}] {proj}: {snip}…")
    else:
        lines.append("  No recent activity in last 48h.")
    lines.append("  Use search_memory(query=...) to retrieve relevant context.")
    lines.append("</memfusion_status>")
    print("\n".join(lines))

asyncio.run(main())
PYEOF
```

### 8b. `prompt_memory_inject.sh` — UserPromptSubmit hook

```bash
#!/usr/bin/env bash
# UserPromptSubmit hook — injects relevant memories above 0.75 score.
VENV="$HOME/.local/share/mem-fusion/venv/bin/python"
QUEUE="$HOME/.local/share/mem-fusion/queue"
SESSION_ID="${CLAUDE_SESSION_ID:-unknown}"
SEEN_FILE="${QUEUE}/seen-${SESSION_ID}.txt"

PROMPT=$(cat)
[[ ${#PROMPT} -lt 15 ]] && exit 0

$VENV - <<PYEOF
import sys, asyncio, os
sys.path.insert(0, "$HOME/.local/share/mem-fusion")
import mcp_server as srv

PROMPT    = """${PROMPT//\"/\\\"}"""
SEEN_FILE = """${SEEN_FILE}"""
MAX_CHARS = 2000
MIN_SCORE = 0.75

async def main():
    seen_ids = set()
    try:
        with open(SEEN_FILE) as f:
            seen_ids = set(f.read().splitlines())
    except FileNotFoundError:
        pass

    result = await srv.tool_search({"query": PROMPT, "top_k": 5, "min_importance": 2})
    hits = [r for r in result.get("results", [])
            if r["score"] >= MIN_SCORE and r["id"] not in seen_ids]
    if not hits:
        return

    lines, total, new_ids = [], 0, []
    for hit in hits:
        entry = f'<memory id="{hit["id"]}" score="{hit["score"]}" type="{hit["type"]}">{hit["content"]}</memory>'
        if total + len(entry) > MAX_CHARS:
            break
        lines.append(entry)
        total += len(entry)
        new_ids.append(hit["id"])

    if not lines:
        return

    with open(SEEN_FILE, "a") as f:
        f.write("\n".join(new_ids) + "\n")

    print("<memory_context>\n" + "\n".join(lines) + "\n</memory_context>")

asyncio.run(main())
PYEOF
```

### 8c. `capture_file_write.sh` — PostToolUse:Write hook

```bash
#!/usr/bin/env bash
# Captures NEW files >100 lines as code memories. Fire-and-forget, backgrounded.
VENV="$HOME/.local/share/mem-fusion/venv/bin/python"
LOG="$HOME/.local/share/mem-fusion/logs/ingest.log"

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('file_path',''))" 2>/dev/null)

[[ -z "$FILE_PATH" || ! -f "$FILE_PATH" ]] && exit 0

LINE_COUNT=$(wc -l < "$FILE_PATH" 2>/dev/null || echo 0)
[[ "$LINE_COUNT" -lt 100 ]] && exit 0

PROJECT=$(basename "$(pwd)")

nohup $VENV - <<PYEOF >> "$LOG" 2>&1 &
import sys, asyncio
sys.path.insert(0, "$HOME/.local/share/mem-fusion")
import mcp_server as srv

async def main():
    path  = """${FILE_PATH}"""
    lines = ${LINE_COUNT}
    proj  = """${PROJECT}"""
    with open(path) as f:
        head = "".join(f.readlines()[:10]).strip()[:300]
    content = f"New file written: {path} ({lines} lines)\n\nHeader:\n{head}"
    result = await srv.tool_store({
        "content": content, "type": "code", "project": proj,
        "importance": 3, "tags": ["file-write"],
    })
    print(f"capture_file_write: {result}")

asyncio.run(main())
PYEOF

exit 0
```

### 8d. `ingest_session.py` — Stop hook (Python, not shell)

Save to `~/.local/share/mem-fusion/scripts/ingest_session.py`:

```python
#!/usr/bin/env python3
"""Session ingestion — Stop hook. Backgrounded; doesn't block exit."""
import json, logging, os, re, sys, hashlib
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR   = Path.home() / ".local/share/mem-fusion"
LOG_PATH     = MEMORY_DIR / "logs/ingest.log"
SESSIONS_DIR = Path.home() / ".claude/sessions"
DEDUP_FILE   = MEMORY_DIR / "queue/ingested_sessions.txt"
QUEUE_DIR    = MEMORY_DIR / "queue"

logging.basicConfig(filename=str(LOG_PATH), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest-session")


def load_dedup_registry():
    if DEDUP_FILE.exists():
        return set(DEDUP_FILE.read_text().splitlines())
    return set()


def mark_ingested(session_id):
    with open(DEDUP_FILE, "a") as f:
        f.write(session_id + "\n")


def find_latest_session():
    if not SESSIONS_DIR.exists():
        return None, []
    files = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None, []
    latest = files[0]
    sid    = latest.stem
    msgs   = []
    try:
        for line in latest.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except Exception as e:
        log.error("Failed to read session file: %s", e)
    return sid, msgs


def assess_session_quality(messages):
    if not messages:
        return {"skip": True, "reason": "empty"}
    user_turns = sum(1 for m in messages if m.get("role") == "human")
    asst_turns = sum(1 for m in messages if m.get("role") == "assistant")
    tool_calls = sum(1 for m in messages if m.get("role") == "assistant"
                     and any(isinstance(c, dict) and c.get("type") == "tool_use"
                             for c in (m.get("content") if isinstance(m.get("content"), list) else [])))
    write_edit = sum(1 for m in messages if m.get("role") == "assistant"
                     and any(isinstance(c, dict) and c.get("type") == "tool_use"
                             and c.get("name") in ("Write", "Edit", "write", "edit")
                             for c in (m.get("content") if isinstance(m.get("content"), list) else [])))
    if user_turns < 4:
        return {"skip": True, "reason": f"only {user_turns} user turns (< 4)"}
    if write_edit >= 5:    importance = 4
    elif write_edit >= 2 or tool_calls >= 5: importance = 3
    else:                  importance = 2
    return {"skip": False, "importance": importance, "tool_calls": tool_calls,
            "write_edit_calls": write_edit, "user_turns": user_turns}


def extract_text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    for sub in (block.get("content") or []):
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append(sub.get("text", "")[:200])
        return " ".join(parts)
    return ""


DECISION_PATTERNS = [
    r"we (?:decided|chose|picked|selected|went with|are going with|will use)\b.{10,120}",
    r"(?:decided|choosing|selecting) to\b.{10,120}",
    r"going with\b.{10,100}",
]
ERROR_RESOLUTION_PATTERNS = [
    r"(?:fixed|resolved|solved|the fix (?:is|was))\b.{10,150}",
    r"(?:the issue was|root cause)\b.{10,150}",
]
PREFERENCE_PATTERNS = [
    r"(?:always|never|don't|do not|please|prefer)\b.{10,100}",
    r"(?:i want|i'd like|i prefer)\b.{10,100}",
    r"next time\b.{10,100}",
]


def extract_signals(messages, session_id):
    memories = []
    full_text = []
    for msg in messages:
        role    = msg.get("role", "")
        content = extract_text_content(msg.get("content", ""))
        if not content:
            continue
        full_text.append(f"[{role}]: {content[:500]}")
        if role == "human":
            for pat in PREFERENCE_PATTERNS:
                for m in re.finditer(pat, content, re.IGNORECASE):
                    s = m.group(0).strip()[:200]
                    if len(s) > 20:
                        memories.append({"content": s, "type": "preference",
                                         "source": "hook", "session_id": session_id})
        elif role == "assistant":
            for pat in DECISION_PATTERNS:
                for m in re.finditer(pat, content, re.IGNORECASE):
                    s = m.group(0).strip()[:250]
                    if len(s) > 25:
                        memories.append({"content": s, "type": "decision",
                                         "source": "hook", "session_id": session_id})
            for pat in ERROR_RESOLUTION_PATTERNS:
                for m in re.finditer(pat, content, re.IGNORECASE):
                    s = m.group(0).strip()[:250]
                    if len(s) > 25:
                        memories.append({"content": s, "type": "error",
                                         "source": "hook", "session_id": session_id})

    seen = set()
    unique = []
    for mem in memories:
        h = hashlib.sha256(mem["content"].strip().lower().encode()).hexdigest()[:16]
        if h not in seen:
            seen.add(h)
            unique.append(mem)

    if full_text:
        unique.insert(0, {"content": f"[Session {session_id[:8]}] {chr(10).join(full_text[:6])[:600]}",
                          "type": "session", "source": "hook", "session_id": session_id})
    return unique


def store_via_api(memories, importance, project):
    import asyncio, urllib.request
    sys.path.insert(0, str(MEMORY_DIR))
    try:
        urllib.request.urlopen("http://127.0.0.1:6333/healthz", timeout=3)
    except Exception:
        log.error("Qdrant not reachable — queueing memories")
        for mem in memories:
            mem["importance"] = importance
            mem["project"]    = project
            ts   = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            h    = hashlib.sha256(mem["content"].encode()).hexdigest()[:8]
            (QUEUE_DIR / f"{ts}-{h}.json").write_text(json.dumps(mem))
        return

    import mcp_server as srv
    stored = 0
    for mem in memories:
        try:
            r = asyncio.run(srv.tool_store({**mem, "importance": importance, "project": project}))
            if r.get("status") == "stored":
                stored += 1
        except Exception as e:
            log.error("Store failed: %s — %s", mem["content"][:40], e)
    log.info("Ingestion complete: %d/%d stored", stored, len(memories))


def main():
    log.info("=== Session ingestion started ===")
    project = os.getenv("CLAUDE_PROJECT", "")
    if not project:
        cwd = os.getcwd()
        project = Path(cwd).name if cwd != str(Path.home()) else "general"

    sid, messages = find_latest_session()
    if not sid:
        return

    if sid in load_dedup_registry():
        return

    q = assess_session_quality(messages)
    if q.get("skip"):
        mark_ingested(sid)
        return

    memories = extract_signals(messages, sid)
    if memories:
        store_via_api(memories, importance=q["importance"], project=project)
    mark_ingested(sid)
    log.info("=== Session ingestion complete ===")


if __name__ == "__main__":
    main()
```

After writing all four:

```bash
chmod +x ~/.local/share/mem-fusion/scripts/*.sh
chmod +x ~/.local/share/mem-fusion/scripts/ingest_session.py
```

---

## Step 9 — Write the launchd plists

Detect the user's actual `$HOME` and arch, then write the plists. Plists do **not** expand env vars in path strings, so `$HOME` must be substituted at write time.

### 9a. `~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist`

```bash
HOME_LIT="$HOME"
cat > ~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.branchapp.memfusion.qdrant</string>
    <key>ProgramArguments</key>
    <array>
        <string>${HOME_LIT}/.local/share/mem-fusion/bin/qdrant</string>
        <string>--config-path</string>
        <string>${HOME_LIT}/.local/share/mem-fusion/qdrant-config.yaml</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>${HOME_LIT}/.local/share/mem-fusion/logs/qdrant.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME_LIT}/.local/share/mem-fusion/logs/qdrant-error.log</string>
    <key>SoftResourceLimits</key>
    <dict><key>NumberOfFiles</key><integer>65536</integer></dict>
    <key>HardResourceLimits</key>
    <dict><key>NumberOfFiles</key><integer>65536</integer></dict>
    <key>WorkingDirectory</key>
    <string>${HOME_LIT}/.local/share/mem-fusion</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>${HOME_LIT}</string>
        <key>MALLOC_CONF</key><string>background_thread:false</string>
    </dict>
</dict>
</plist>
EOF
```

The `MALLOC_CONF=background_thread:false` and `NumberOfFiles=65536` are **required** — Qdrant won't start reliably without them on macOS.

### 9b. `~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist`

```bash
HOME_LIT="$HOME"
if [[ "$(uname -m)" == "arm64" ]]; then
  OLLAMA_BIN="/opt/homebrew/bin/ollama"
else
  OLLAMA_BIN="/usr/local/bin/ollama"
fi

cat > ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.branchapp.memfusion.ollama</string>
    <key>ProgramArguments</key>
    <array>
        <string>${OLLAMA_BIN}</string>
        <string>serve</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>${HOME_LIT}/.local/share/mem-fusion/logs/ollama.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME_LIT}/.local/share/mem-fusion/logs/ollama-error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>${HOME_LIT}</string>
        <key>OLLAMA_HOST</key><string>127.0.0.1:11434</string>
    </dict>
</dict>
</plist>
EOF
```

---

## Step 10 — Start the services and pull the embedding model

```bash
launchctl load ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist
launchctl load ~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist

echo "Waiting for Ollama..."
curl -s --retry 12 --retry-delay 1 --retry-connrefused http://127.0.0.1:11434/api/tags >/dev/null \
  && echo "  Ollama up" || { echo "Ollama failed to start — check logs"; exit 1; }

echo "Waiting for Qdrant..."
curl -s --retry 12 --retry-delay 1 --retry-connrefused http://127.0.0.1:6333/healthz >/dev/null \
  && echo "  Qdrant up" || { echo "Qdrant failed to start — check logs"; exit 1; }

ollama pull nomic-embed-text

~/.local/share/mem-fusion/venv/bin/python \
  ~/.local/share/mem-fusion/scripts/init_collection.py
```

---

## Step 11 — Register the MCP with Claude Code

```bash
claude mcp add mem-fusion \
  ~/.local/share/mem-fusion/venv/bin/python \
  ~/.local/share/mem-fusion/mcp_server.py

claude mcp list | grep mem-fusion
```

You should see `mem-fusion: ... - ✓ Connected`.

---

## Step 12 — Wire up the 4 hooks in `~/.claude/settings.json`

```bash
python3 - <<'PYEOF'
import json, os
from pathlib import Path

settings_path = Path.home() / ".claude/settings.json"
home          = str(Path.home())

if settings_path.exists():
    settings = json.loads(settings_path.read_text())
else:
    settings = {}

new_hooks = {
    "SessionStart": [{
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": f"{home}/.local/share/mem-fusion/scripts/session_prime.sh",
            "timeout": 8,
        }],
    }],
    "UserPromptSubmit": [{
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": f"{home}/.local/share/mem-fusion/scripts/prompt_memory_inject.sh",
            "timeout": 2,
        }],
    }],
    "Stop": [{
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": (
                f"nohup {home}/.local/share/mem-fusion/venv/bin/python "
                f"{home}/.local/share/mem-fusion/scripts/ingest_session.py "
                f">> {home}/.local/share/mem-fusion/logs/ingest.log 2>&1 & "
                f"rm -f {home}/.local/share/mem-fusion/queue/seen-${{CLAUDE_SESSION_ID}}.txt"
            ),
            "timeout": 3,
        }],
    }],
    "PostToolUse": [{
        "matcher": "Write",
        "hooks": [{
            "type": "command",
            "command": f"{home}/.local/share/mem-fusion/scripts/capture_file_write.sh",
            "timeout": 2,
        }],
    }],
}

existing_hooks = settings.get("hooks", {})
for event, blocks in new_hooks.items():
    existing_blocks = existing_hooks.get(event, [])
    for new_block in blocks:
        is_dupe = any(
            eb.get("matcher") == new_block["matcher"]
            and any(h.get("command") == new_block["hooks"][0]["command"]
                    for h in eb.get("hooks", []))
            for eb in existing_blocks
        )
        if not is_dupe:
            existing_blocks.append(new_block)
    existing_hooks[event] = existing_blocks
settings["hooks"] = existing_hooks

settings_path.write_text(json.dumps(settings, indent=2))
print(f"Hooks merged into {settings_path}")
PYEOF
```

---

## Step 13 — Install the `/remember` skill

```bash
mkdir -p ~/.claude/skills/remember
cat > ~/.claude/skills/remember/SKILL.md <<'EOF'
---
name: remember
description: Store a specific piece of information into the persistent vector memory system
trigger: User says "remember this", "remember that", "/remember", or asks Claude to save something for future sessions
---

# /remember — Store to Persistent Memory

When this skill is triggered, store the specified content into the vector memory system
with `importance=5` (user-curated, highest priority) and confirm back to the user
what was stored and its ID.

## Protocol

1. Identify WHAT to remember:
   - If the user specified content explicitly, use that verbatim
   - If the user said "remember this" without specifying, summarize the current context
     into a clear, self-contained statement (1–3 sentences)

2. Classify the memory TYPE:
   - `decision`   — a choice was made (architecture, approach, tool selection)
   - `preference` — user expressed how they want things done
   - `fact`       — factual information about a project or system
   - `error`      — an error that was resolved and how
   - `code`       — a significant code pattern or implementation
   - `context`    — background context about a project or initiative

3. Call `store_memory` with:
   - `importance: 5`
   - Infer `project` from conversation context if not stated
   - Infer `tags` from the content topic

4. Confirm to the user:
   ```
   Remembered: [brief summary]
   ID: [memory_id]
   Type: [type] | Project: [project] | Tags: [tags]
   ```
EOF
```

---

## Step 14 — Smoke test

This proves the whole stack works. **Run it before reporting success.**

```bash
launchctl list | grep -E "com.branchapp.memfusion" | head
curl -s http://127.0.0.1:6333/healthz
curl -s http://127.0.0.1:11434/api/tags | python3 -c "import sys,json; print('Ollama models:', [m['name'] for m in json.load(sys.stdin)['models']])"

claude mcp list | grep mem-fusion

~/.local/share/mem-fusion/venv/bin/python - <<'PYEOF'
import asyncio, sys
sys.path.insert(0, str(__import__('pathlib').Path.home() / ".local/share/mem-fusion"))
import mcp_server as srv

async def main():
    r1 = await srv.tool_store({
        "content": "Smoke test: Mem-Fusion install completed on " + __import__('datetime').datetime.now().isoformat(),
        "type": "context", "importance": 3, "project": "install-test",
        "tags": ["smoke-test"],
    })
    print("STORE:", r1)

    r2 = await srv.tool_search({"query": "smoke test install", "top_k": 3})
    print("SEARCH count:", r2["count"])
    assert r2["count"] >= 1, "Search didn't find the just-stored memory"

    r3 = await srv.tool_stats({})
    print("STATS:", r3)

    print("\n✓ All smoke tests passed")

asyncio.run(main())
PYEOF
```

---

## Step 15 — Tell the user what to add to their CLAUDE.md

After install, the user needs to teach **their** Claude how to use the memory tools. Print this snippet and instruct them to paste it into `~/CLAUDE.md` (or a project-level `CLAUDE.md`):

```markdown
## Vector Memory System

Connected to a local `mem-fusion` MCP server (Qdrant + nomic-embed-text on localhost).
**Check this at session start** by calling `memory_stats()` to confirm the system is live.

### When to search
- **Session start**: `search_memory(query="<current task or project name>", top_k=8)`
- **Before architectural decisions**: search for prior decisions on the same topic
- **When hitting a recurring error**: search for prior resolutions
- **When unsure about a user preference**: search type="preference"

### When to store
- **Decision made**: `store_memory(content, type="decision", importance=4, project="<name>")`
- **Novel error resolved**: `store_memory(..., type="error", importance=3)`
- **User reveals a preference**: `store_memory(..., type="preference", importance=4)`
- **Important context learned**: `store_memory(..., type="context", importance=3)`
- **User explicitly asks to remember**: use `/remember` skill → `importance=5`

### What NOT to store
Trivial facts, transient state, things derivable from code or `git log`.

### Memory types
`decision` · `fact` · `preference` · `error` · `code` · `context` · `session`

### Available tools
`store_memory` · `search_memory` · `search_recent` · `upsert_memory` ·
`find_or_create` · `delete_memory` · `get_related` · `memory_stats`

### Verification rule
Memories are point-in-time observations. Before recommending a file/function/flag named in
a memory, verify it still exists in the current code.
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `claude mcp list` shows mem-fusion but `Failed to connect` | Check the venv exists and `mcp_server.py` runs without import errors: `~/.local/share/mem-fusion/venv/bin/python ~/.local/share/mem-fusion/mcp_server.py` (should hang waiting for stdio — kill with Ctrl-C). Look at `~/.local/share/mem-fusion/logs/mcp.log`. |
| Qdrant won't start | Confirm `MALLOC_CONF=background_thread:false` is in the plist `EnvironmentVariables` and `NumberOfFiles=65536` is set. Check `~/.local/share/mem-fusion/logs/qdrant-error.log`. |
| Search returns 0 results | Verify Ollama is up: `curl http://127.0.0.1:11434/api/tags`. Verify the model is pulled: `ollama list \| grep nomic-embed-text`. |
| Hooks not firing | Confirm `~/.claude/settings.json` was merged correctly (Step 12). Run scripts manually to surface any errors: `~/.local/share/mem-fusion/scripts/session_prime.sh`. |
| Session ingestion logging "no session file found" | Newer Claude Code may have moved session files. Check `~/.claude/sessions/*.jsonl` exists. If the path differs, edit `SESSIONS_DIR` in `ingest_session.py`. |

Logs directory: `~/.local/share/mem-fusion/logs/` — `mcp.log`, `qdrant.log`, `qdrant-error.log`, `ollama.log`, `ingest.log`.

---

## Uninstall (clean removal)

```bash
claude mcp remove mem-fusion

launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist 2>/dev/null
launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist 2>/dev/null
rm ~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist

echo "Edit ~/.claude/settings.json — remove the 4 mem-fusion entries from .hooks, or restore from the .bak file made in Pre-flight."

read -p "Delete all stored memories? [y/N] " yn
[[ "$yn" == "y" ]] && rm -rf ~/.local/share/mem-fusion

rm -rf ~/.claude/skills/remember
```

---

## What's intentionally NOT in this install

- **No telemetry / phone-home.** Everything is localhost-only.
- **No Linux/Windows support.** macOS launchd is hard-required by the architecture; porting is future work.
- **No automatic upgrade path.** To upgrade, re-run this prompt — `init_collection.py` and the duplicate-hash check make it idempotent.

---

## After install — final report to the user

When all 14 steps + smoke test pass, tell the user:

> Your Mem-Fusion vector memory system is live. **Restart your Claude Code session** for the hooks to take effect. Then paste the CLAUDE.md snippet from Step 15 into your `~/CLAUDE.md`. Try it out by saying *"remember that I prefer Python virtual envs created with `uv`"* — Claude should call the `/remember` skill and confirm with a memory ID.
