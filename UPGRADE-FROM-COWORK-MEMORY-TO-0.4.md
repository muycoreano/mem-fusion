# Upgrade cowork-memory → Mem-Fusion v0.4 — Setup Prompt for Claude Code

> **How to use this file:** Open Claude Code in a terminal on your Mac and paste this entire document as your first message. Claude will detect your existing cowork-memory install, upgrade it to mem-fusion v0.4 in place, and preserve every stored memory. The running Qdrant and Ollama daemons are reused; nothing about your data location changes.

---

## What this upgrade does

- Installs v0.4 mem-fusion code at `~/.local/share/mem-fusion/` (new directory, parallel to existing cowork-memory)
- **Reuses the running Qdrant and Ollama** — same data dir, same `cowork_memories` collection, same `nomic-embed-text` model
- Runs the v0.3 → v0.4 data migration — idempotent backfill of `groups: ["personal"]` on every existing entry
- Swaps the `cowork-memory` MCP registration → `mem-fusion`
- Rewires the four Claude Code hooks from cowork-memory paths to mem-fusion paths
- Installs the v0.4 `/remember` skill (explicit group-keyed tagging)
- Leaves the cowork-memory install on disk as a rollback safety net

## What this upgrade does NOT do

- Move the Qdrant data directory (it stays at `~/.local/share/cowork-memory/qdrant-data/` — see "Long-term cleanup" at the bottom for moving it later)
- Stop or modify the cowork-memory Qdrant / Ollama launchd plists (`com.cowork.qdrant.plist`, `com.cowork.ollama.plist`)
- Install Constellation (run `INSTALL_CONSTELLATION.md` separately for that)
- Delete any cowork-memory files
- Touch your `~/CLAUDE.md` (you'll update that yourself after — instructions in Step 11)

Time + disk: about 5 minutes, ~50 MB additional disk (a new venv + the mem-fusion code).

---

## Pre-flight — verify before starting

```bash
# 1. macOS only
[[ "$(uname)" == "Darwin" ]] || { echo "macOS only"; exit 1; }

# 2. cowork-memory install present
[[ -d "$HOME/.local/share/cowork-memory" ]] || {
  echo "ERROR: no cowork-memory install at ~/.local/share/cowork-memory"
  echo "       This document upgrades an existing cowork-memory install."
  echo "       For a fresh install, use INSTALL_MEM_FUSION.md instead."
  exit 1
}
[[ -x "$HOME/.local/share/cowork-memory/venv/bin/python" ]] || {
  echo "ERROR: cowork-memory venv missing or unusable — manual recovery required"
  exit 1
}

# 3. Existing Qdrant + Ollama running
curl -s http://127.0.0.1:6333/healthz   >/dev/null || { echo "Qdrant not reachable on 6333 — start it first"; exit 1; }
curl -s http://127.0.0.1:11434/api/tags >/dev/null || { echo "Ollama not reachable on 11434 — start it first"; exit 1; }

# 4. cowork-memory MCP currently registered
if ! claude mcp list 2>/dev/null | grep -q "^cowork-memory:"; then
  echo "WARN: cowork-memory MCP not registered with Claude — upgrade may already be partial"
fi

# 5. No prior mem-fusion install
[[ -f "$HOME/.local/share/mem-fusion/mem_fusion.py" ]] && \
  echo "WARN: mem-fusion already installed at ~/.local/share/mem-fusion/ — will overwrite"

# 6. Backup Claude settings before we touch hooks
[[ -f ~/.claude/settings.json ]] && cp ~/.claude/settings.json ~/.claude/settings.json.bak.$(date +%Y%m%d-%H%M%S)
```

If any of 1–3 fails, **stop and ask the user** before continuing. Don't proceed past pre-flight on errors.

---

## Step 1 — Report memory count

```bash
COUNT=$(curl -s -X POST http://127.0.0.1:6333/collections/cowork_memories/points/count \
        -H 'Content-Type: application/json' -d '{"exact":true}' \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["count"])')
echo "→ You have $COUNT memories in cowork-memory. All will be preserved."
```

If this errors or returns 0, stop — Qdrant may be unhealthy or the collection has a different name.

---

## Step 2 — Lay out the mem-fusion directory

```bash
mkdir -p ~/.local/share/mem-fusion/{scripts,logs,queue,snapshots}
```

---

## Step 3 — Python venv

```bash
python3.12 -m venv ~/.local/share/mem-fusion/venv 2>/dev/null \
  || python3.11 -m venv ~/.local/share/mem-fusion/venv \
  || python3 -m venv ~/.local/share/mem-fusion/venv

~/.local/share/mem-fusion/venv/bin/pip install --upgrade pip
~/.local/share/mem-fusion/venv/bin/pip install \
  mcp==1.6.0 qdrant-client==1.13.1 httpx==0.28.1

cat > ~/.local/share/mem-fusion/requirements.txt <<'EOF'
mcp==1.6.0
qdrant-client==1.13.1
httpx==0.28.1
EOF
```

---

## Step 4 — Install the mem-fusion code

```bash
cat > ~/.local/share/mem-fusion/mem_fusion.py <<'F01E42F0C765_EOF'
#!/usr/bin/env python3
"""
Mem-Fusion MCP Server — stdio transport for Claude Code.

Thin proxy over `core.py`. Registers 11 memory tools with the MCP server
and delegates each one to the corresponding `core` function (for local
memory ops) or to the local Constellation gateway over HTTP (for group
ops). No business logic lives in this file.

The two group tools (`group_pull`, `group_push`) call Constellation's
local gateway at 127.0.0.1:7534. If Constellation isn't installed/running,
the calls fail cleanly with {"error": "constellation_not_installed"} and
Claude can tell the user to install Constellation if they want group memory.
"""
import asyncio
import json
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

import core


log = core.configure_logging("mem-fusion")

server = Server("mem-fusion")

# Local Constellation gateway. Override with MEMFUSION_CONSTELLATION_GATEWAY.
CONSTELLATION_GATEWAY = os.getenv("MEMFUSION_CONSTELLATION_GATEWAY",
                                  "http://127.0.0.1:7534")
GATEWAY_TIMEOUT_S = 30.0


@server.list_tools()
async def list_tools():
    type_enum = ["decision","fact","preference","error","code","context","session"]
    return [
        Tool(name="store_memory",
             description=("Store a new memory in the persistent vector database. "
                          "Call after any decision, discovery, user preference, error resolution, "
                          "or substantial code written. Do NOT store trivial facts or transient state. "
                          "type one of: decision, fact, preference, error, code, context, session. "
                          "importance: 1=trivial, 3=normal, 4=important, 5=critical (user-curated only). "
                          "groups: list of group tags for sharing — defaults to ['personal'] "
                          "(local-only on this peer). Pass explicit groups to share with teammates."),
             inputSchema={"type": "object", "properties": {
                 "content":    {"type": "string"},
                 "type":       {"type": "string", "enum": type_enum},
                 "tags":       {"type": "array", "items": {"type": "string"}},
                 "project":    {"type": "string"},
                 "importance": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
                 "session_id": {"type": "string"},
                 "groups":     {"type": "array", "items": {"type": "string"},
                                "description": "Group tags for sharing. Default ['personal']."},
             }, "required": ["content", "type"]}),
        Tool(name="search_memory",
             description=("Semantic search across all stored memories. Call at session start with the current task, "
                          "before architectural decisions, and on recurring errors. Scores >0.75 are meaningful."),
             inputSchema={"type": "object", "properties": {
                 "query":          {"type": "string"},
                 "top_k":          {"type": "integer", "default": 8},
                 "project":        {"type": "string"},
                 "type":           {"type": "string", "enum": type_enum},
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
             description="Update an existing memory by ID. Preserves groups — use add_groups to widen sharing.",
             inputSchema={"type": "object", "properties": {
                 "id":         {"type": "string"},
                 "content":    {"type": "string"},
                 "type":       {"type": "string", "enum": type_enum},
                 "tags":       {"type": "array", "items": {"type": "string"}},
                 "importance": {"type": "integer", "minimum": 1, "maximum": 5},
             }, "required": ["id", "content"]}),
        Tool(name="find_or_create",
             description=("Search first, store if no result above 0.82. If found, additively merges "
                          "the provided groups into the existing entry's groups."),
             inputSchema={"type": "object", "properties": {
                 "content":    {"type": "string"},
                 "type":       {"type": "string", "enum": type_enum},
                 "tags":       {"type": "array", "items": {"type": "string"}},
                 "project":    {"type": "string"},
                 "importance": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
                 "groups":     {"type": "array", "items": {"type": "string"}},
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
        Tool(name="export_record",
             description=("Return a stored memory's full Qdrant record by ID, including its "
                          "768-dim vector. Used by Constellation and other extensions that need "
                          "faithful memory propagation across machines (vector copied verbatim, "
                          "not re-embedded)."),
             inputSchema={"type": "object", "properties": {
                 "id": {"type": "string", "description": "Memory ID from a prior store/search result"},
             }, "required": ["id"]}),
        Tool(name="add_groups",
             description=("Additively widen the group set on one or more existing memories. "
                          "Used for the store-now-share-later workflow: store memories "
                          "(default groups=['personal']), then later add a shared group "
                          "before calling group_push. Never removes a group — only adds. "
                          "Returns {updated, no_op, errors} telemetry; render a brief summary."),
             inputSchema={"type": "object", "properties": {
                 "memory_ids": {"type": "array", "items": {"type": "string"},
                                "description": "IDs from prior store_memory / search results"},
                 "groups":     {"type": "array", "items": {"type": "string"},
                                "description": "Groups to add (additive union)"},
             }, "required": ["memory_ids", "groups"]}),
        Tool(name="group_pull",
             description=("Pull new memories from peers via the local Constellation daemon. "
                          "Omit `group` to iterate every configured group with peers; pass "
                          "`group=<name>` to pull from one group only. Returns per-peer telemetry "
                          "{peers: [{node_name, group_name, status: responsive|unreachable, "
                          "entry_ids?, merged_ids?, reason?}]}. Render a per-peer natural-language "
                          "summary; never dump the raw JSON. Requires Constellation."),
             inputSchema={"type": "object", "properties": {
                 "group": {"type": "string",
                           "description": "Optional. Limit pull to this group only."},
             }, "required": []}),
        Tool(name="group_push",
             description=("Share local memories with peers in a group via the local Constellation "
                          "daemon. Always scoped to one group per call. Pass `memory_ids` to push "
                          "specific memories (use this for share-after-the-fact flows after add_groups); "
                          "omit to push every local entry tagged with that group (bulk catch-up). "
                          "Returns per-peer-per-memory telemetry; render prose, never dump JSON. "
                          "Push scope rule: only contacts peers in the named group, even if memories "
                          "are also tagged for other groups. Requires Constellation."),
             inputSchema={"type": "object", "properties": {
                 "group":      {"type": "string",
                                "description": "Target group; must be a configured membership."},
                 "memory_ids": {"type": "array", "items": {"type": "string"},
                                "description": "Optional. Memory IDs to push; omit for bulk push of the whole group."},
             }, "required": ["group"]}),
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
    if name == "store_memory":   return await core.store_memory(args)
    if name == "search_memory":  return await core.search_memory(args)
    if name == "search_recent":  return await core.search_recent(args)
    if name == "upsert_memory":  return await core.upsert_memory(args)
    if name == "find_or_create": return await core.find_or_create(args)
    if name == "delete_memory":  return await core.delete_memory(args)
    if name == "get_related":    return await core.get_related(args)
    if name == "memory_stats":   return await core.memory_stats(args)
    if name == "export_record":  return await core.export_record(args)
    if name == "add_groups":     return await core.add_groups(args)
    if name == "group_pull":     return await group_pull(args)
    if name == "group_push":     return await group_push(args)
    raise ValueError(f"Unknown tool: {name}")


# ── Group tools — thin proxies to local Constellation gateway ────────────
async def _post_gateway(path: str, body: dict) -> dict:
    """Common error-mapping for gateway POSTs. Caller passes the body."""
    try:
        async with httpx.AsyncClient(timeout=GATEWAY_TIMEOUT_S) as client:
            r = await client.post(f"{CONSTELLATION_GATEWAY}{path}", json=body)
        if r.status_code != 200:
            return {"error": "gateway_error",
                    "detail": f"http {r.status_code}: {r.text[:200]}"}
        return r.json()
    except httpx.ConnectError:
        return {"error": "constellation_not_installed",
                "detail": f"could not reach gateway at {CONSTELLATION_GATEWAY}"}
    except httpx.TimeoutException:
        return {"error": "gateway_timeout",
                "detail": f"gateway did not respond within {GATEWAY_TIMEOUT_S}s"}


async def group_pull(args):
    """POST /pull on local Constellation gateway.

    Body: {group?: str}. Without `group`, pulls from every configured
    membership with peers. With `group`, pulls only that group's peers.
    """
    body = {}
    if args.get("group"):
        body["group"] = args["group"]
    return await _post_gateway("/pull", body)


async def group_push(args):
    """POST /push on local Constellation gateway.

    Body: {group: str, memory_ids?: list[str]}. The gateway scrolls the
    matching local entries and fans them out to the named group's peers,
    applying the push-time group filter on the wire.
    """
    group = args.get("group")
    if not group:
        return {"error": "missing_argument", "detail": "group is required"}
    body = {"group": group}
    if args.get("memory_ids"):
        body["memory_ids"] = args["memory_ids"]
    return await _post_gateway("/push", body)


async def main():
    log.info("Mem-Fusion MCP Server starting")
    try:
        core.migrate_legacy_entries(log)
    except Exception as e:
        log.warning("legacy migration failed at startup (continuing): %s", e)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
F01E42F0C765_EOF
chmod +x ~/.local/share/mem-fusion/mem_fusion.py
```

```bash
cat > ~/.local/share/mem-fusion/core.py <<'3B2A2906F11A_EOF'
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
import json
import logging
import os
import socket
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
# The collection name is invariant. Every entry carries a `groups` payload
# (list[str]); routing happens by group membership, not by collection name.
COLLECTION  = "cowork_memories"
EMBED_MODEL = "nomic-embed-text"
VECTOR_SIZE = 768

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
       {error: "ollama_unreachable"}     — embed failed; caller surfaces error
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
    if vec is None:
        return {"error": "ollama_unreachable",
                "detail": "Local Ollama did not respond; cannot embed. Verify Ollama is running."}

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
    """Update an existing point by id. Re-embeds the new content. Groups are
    preserved (use add_groups to widen sharing)."""
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
    if vec is None:
        return {"error": "ollama_unreachable",
                "detail": "Local Ollama did not respond; cannot embed for find_or_create."}

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
3B2A2906F11A_EOF
```

---

## Step 5 — Install the v0.4 hook scripts

The script names are identical to cowork-memory's, but the bodies have been rewritten for v0.4 (everything captured by hooks defaults to `groups: ["personal"]`; no auto-classification).

```bash
cat > ~/.local/share/mem-fusion/scripts/session_prime.sh <<'654FC978BF5B_EOF'
#!/usr/bin/env bash
# SessionStart hook — emits a brief recent-memory snapshot for context priming.
VENV="$HOME/.local/share/mem-fusion/venv/bin/python"

$VENV - <<PYEOF
import sys, asyncio
sys.path.insert(0, "$HOME/.local/share/mem-fusion")
import core

async def main():
    recent = await core.search_recent({"hours": 48, "top_k": 5})
    stats  = await core.memory_stats({})
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
654FC978BF5B_EOF
chmod +x ~/.local/share/mem-fusion/scripts/session_prime.sh
```

```bash
cat > ~/.local/share/mem-fusion/scripts/prompt_memory_inject.sh <<'43A517C220BA_EOF'
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
import core

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

    result = await core.search_memory({"query": PROMPT, "top_k": 5, "min_importance": 2})
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
43A517C220BA_EOF
chmod +x ~/.local/share/mem-fusion/scripts/prompt_memory_inject.sh
```

```bash
cat > ~/.local/share/mem-fusion/scripts/capture_file_write.sh <<'513F277BB09A_EOF'
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
import core

async def main():
    path  = """${FILE_PATH}"""
    lines = ${LINE_COUNT}
    proj  = """${PROJECT}"""
    with open(path) as f:
        head = "".join(f.readlines()[:10]).strip()[:300]
    content = f"New file written: {path} ({lines} lines)\n\nHeader:\n{head}"
    result = await core.store_memory({
        "content": content, "type": "code", "project": proj,
        "importance": 3, "tags": ["file-write"],
        "groups": ["personal"],
    })
    print(f"capture_file_write: {result}")

asyncio.run(main())
PYEOF

exit 0
513F277BB09A_EOF
chmod +x ~/.local/share/mem-fusion/scripts/capture_file_write.sh
```

```bash
cat > ~/.local/share/mem-fusion/scripts/ingest_session.py <<'C39D19C30496_EOF'
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

    # Hook-captured memories are always personal — they record this peer's
    # work and should never propagate to teammates without explicit user intent.
    import core
    stored = 0
    for mem in memories:
        try:
            r = asyncio.run(core.store_memory({
                **mem, "importance": importance, "project": project,
                "groups": ["personal"],
            }))
            if r.get("status") in ("stored", "merged"):
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
C39D19C30496_EOF
chmod +x ~/.local/share/mem-fusion/scripts/ingest_session.py
```

---

## Step 6 — Run the v0.3 → v0.4 data migration

Backfills `groups: ["personal"]` on every existing entry that doesn't yet have a `groups` payload. Idempotent — safe to re-run. The local Qdrant data is unchanged otherwise.

```bash
~/.local/share/mem-fusion/venv/bin/python -c "
import sys
sys.path.insert(0, '$HOME/.local/share/mem-fusion')
import core
r = core.migrate_legacy_entries()
print(f'Migration: scanned={r[\"scanned\"]}  migrated={r[\"migrated\"]}  skipped={r[\"skipped\"]}')
"
```

Expect `migrated` to equal your total memory count on the first run, `0` on subsequent runs.

---

## Step 7 — Swap the MCP registration

```bash
claude mcp remove cowork-memory 2>/dev/null || true
claude mcp add mem-fusion \
  ~/.local/share/mem-fusion/venv/bin/python \
  ~/.local/share/mem-fusion/mem_fusion.py

claude mcp list | grep mem-fusion
```

Expect `mem-fusion: ... - ✓ Connected`. If it says `Failed to connect`, run the server directly to surface the error:
```bash
~/.local/share/mem-fusion/venv/bin/python ~/.local/share/mem-fusion/mem_fusion.py
# Ctrl-C after — it'll hang waiting for stdio
```

---

## Step 8 — Rewire the four hooks

First, remove the cowork-memory hook entries (so they don't double-fire alongside the mem-fusion ones):

```bash
cat > ~/.local/share/mem-fusion/scripts/unwire_cowork_memory.py <<'41274DFA0974_EOF'
#!/usr/bin/env python3
"""
Remove cowork-memory hook entries from ~/.claude/settings.json.

Run this as part of the cowork-memory → mem-fusion v0.4 upgrade, before
wire_hooks.py installs the v0.4 hook entries. After both scripts run, only
the mem-fusion (and any unrelated) hook entries remain — no double-fire.

Idempotent: re-runs are no-ops once cowork-memory entries are already gone.
"""
import json
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude/settings.json"
COWORK_PATH_FRAGMENT = "/.local/share/cowork-memory/"

if not SETTINGS_PATH.exists():
    print(f"No {SETTINGS_PATH} — nothing to unwire")
    raise SystemExit(0)

settings = json.loads(SETTINGS_PATH.read_text())
hooks = settings.get("hooks", {})

removed = 0
for event in list(hooks.keys()):
    new_blocks = []
    for block in hooks[event]:
        kept_hooks = []
        for h in block.get("hooks", []):
            if COWORK_PATH_FRAGMENT in h.get("command", ""):
                removed += 1
            else:
                kept_hooks.append(h)
        if kept_hooks:
            new_blocks.append({**block, "hooks": kept_hooks})
    if new_blocks:
        hooks[event] = new_blocks
    else:
        del hooks[event]

settings["hooks"] = hooks
SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
print(f"Removed {removed} cowork-memory hook entries from {SETTINGS_PATH}")
41274DFA0974_EOF
chmod +x ~/.local/share/mem-fusion/scripts/unwire_cowork_memory.py
```

```bash
python3 ~/.local/share/mem-fusion/scripts/unwire_cowork_memory.py
```

Then wire in the v0.4 hooks:

```bash
cat > ~/.local/share/mem-fusion/scripts/wire_hooks.py <<'DC4EBE1C3B9C_EOF'
#!/usr/bin/env python3
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
DC4EBE1C3B9C_EOF
chmod +x ~/.local/share/mem-fusion/scripts/wire_hooks.py
```

```bash
python3 ~/.local/share/mem-fusion/scripts/wire_hooks.py
```

Verify the result:
```bash
python3 -c "
import json
s = json.load(open('$HOME/.claude/settings.json'))
for ev, blocks in s.get('hooks', {}).items():
    for b in blocks:
        for h in b.get('hooks', []):
            print(f'{ev:18} -> {h[\"command\"][:80]}')
"
```

Every command path should start with `~/.local/share/mem-fusion/` (or be unrelated to cowork-memory). If any line still contains `/.local/share/cowork-memory/`, the unwire didn't run cleanly — investigate before continuing.

---

## Step 9 — Install the v0.4 `/remember` skill

```bash
mkdir -p ~/.claude/skills/remember
```

```bash
cat > ~/.claude/skills/remember/SKILL.md <<'BEA764C561E7_EOF'
---
name: remember
description: Pin information to persistent memory with explicit group-keyed sharing. Default scope is `personal` (local-only). Sharing happens by group tag — never by content classification.
trigger: User says "remember this", "remember that", "/remember", or asks Claude to save or share something for future sessions
---

# /remember — Store memories with explicit group routing

Every memory carries a `groups` tag that determines who can see it. The default is `personal` (local-only — never leaves this machine). Sharing happens when the user explicitly names a group at store time, or extends the group set after the fact.

There is no content classification. Routing is by user intent only.

## Command surface

```
/remember <content>                              → store with groups=[personal]
/remember <content> for <group>                  → store with groups=[<group>] + push
/remember <content> for <g1>, <g2>, …            → store with groups=[g1,g2,…] + push each
/remember push                                   → bulk push every configured group
/remember push <group>                           → bulk push one group's memories
/remember pull                                   → bulk pull every configured group
/remember pull <group>                           → pull one group's peers
```

Plus the natural-language path Claude handles via reasoning:

| User phrase | Action |
|---|---|
| *"share those memories with `<group>`"* | Resolve *those* from session context, call `add_groups(ids, [group])`, then `group_push(group, memory_ids=ids)`. |
| *"also share them with `<group>`"* | Add the new group, push **only** to that group. Don't re-push prior groups — they already have it. |
| *"make sure all your groups have these"* | For each group in the memory's `groups` list, call `group_push(group, memory_ids=ids)`. |

## Protocol

### 1. Identify what to remember

- If the user specified content explicitly, use that verbatim.
- If the user said *"remember this"* without specifying content, summarize the current conversation context into a clear, self-contained statement.

### 2. Pick the type (decorative, doesn't gate sharing)

Classify into one of: `decision`, `fact`, `preference`, `error`, `code`, `context`. (Don't use `session` — reserved for the Stop hook.) The type is a filter for recall, not a routing key.

### 3. Pick the groups

- If the user said *"for `<group>`"* (or *"for `<g1>`, `<g2>`"*): use those.
- Otherwise: `["personal"]`.

Never invent group names. If the user says *"share with engineering"* and you don't know whether that's `engineering`, `engineering@branch`, or `eng-team`, ask. Cost of asking is low; cost of pushing to the wrong group is a wrong-audience leak.

### 4. Store

Call `mem-fusion/store_memory` with:
- `content`: the content
- `type`: classified above
- `importance: 5` (always for `/remember`)
- `project`: inferred from current context
- `tags`: inferred from content topic
- `groups`: from step 3

Returns `{status: "stored"|"merged"|"duplicate", id, groups}`.

### 5. Push (only if groups other than `personal` were set)

For each non-`personal` group `G` in the memory's groups, call `mem-fusion/group_push(group=G, memory_ids=[id])`.

This is one call per group. Each call only contacts peers in that group. *"Share with product"* never reaches engineering peers, even if the memory is also tagged engineering.

### 6. Confirm to user

Render per-peer prose. Never dump JSON. Example:

```
✓ Stored as decision (id: a7e3c2d1, groups: [personal, engineering@branch]).
✓ Pushed to engineering@branch:
    - bob-mac:       stored
    - alice-desktop: stored
    - carol-laptop:  unreachable (connection refused)
```

If Constellation isn't installed (`group_push` returns `constellation_not_installed`), say so once and continue — local-only is the safe fallback:

```
✓ Stored as decision (id: a7e3c2d1, groups: [personal, engineering@branch]).
ℹ Local only — Constellation not installed; the engineering tag is recorded
   but no peers were notified. Install Constellation to enable sharing.
```

## The store-now-share-later flow

This is the natural workflow and it's load-bearing for v0.4. When the user reflects after work and decides to share what was just stored:

```
User:   "Ok, let's pause and reflect and store the memories."
Claude: [Stores 4 memories with groups=[personal]]
        ✓ Stored 4 memories (all groups=[personal]).
            - decision: gRPC for internal RPC (id: a7e3)
            - decision: Postgres 16 with logical replication (id: b8f4)
            - error:    JWT clock-skew fix (id: c9a5)
            - context:  Auth service migration timeline (id: d0b6)

User:   "Ok, share those with engineering."
Claude: [Resolves "those" → the 4 ids just stored]
        [Calls add_groups(ids, ["engineering@branch"])]
        [Calls group_push(group="engineering@branch", memory_ids=ids)]
        ✓ Added engineering@branch to 4 memories.
        ✓ Pushed to engineering@branch:
            - bob-mac:       4 stored
            - alice-desktop: 4 stored

User:   "Oh, also share them with product."
Claude: [add_groups(ids, ["product@branch"])]
        [group_push(group="product@branch", memory_ids=ids)]   ← product ONLY
        ✓ Added product@branch to 4 memories.
        ✓ Pushed to product@branch:
            - dave-mac:      4 stored
            - eve-laptop:    4 stored
          (Engineering peers not re-contacted.)
```

**Scope rule:** push only to groups the user named in this turn. Don't iterate the memory's full `groups` list. *"Also share with product"* pushes to product peers only.

## Bulk operations

- `/remember pull` — `group_pull()` with no group arg. Iterates every configured group with peers. Render per-peer-per-group prose.
- `/remember pull <group>` — `group_pull(group=<group>)`.
- `/remember push` — for each configured group with peers, call `group_push(group=<group>)` with no `memory_ids` (bulk push of every entry tagged with that group).
- `/remember push <group>` — `group_push(group=<group>)` with no `memory_ids`.

After pull, surface specific arrivals the user asks about with `export_record(id)` or `search_recent`.

## What NOT to do

- **Don't classify content to decide where it goes.** Routing is by the explicit `groups` tag, period. If the user didn't name a group, default is `personal`.
- **Don't push to groups the user didn't name in this turn.** Even if a memory is tagged `[personal, engineering, product]`, *"share with product"* contacts product peers only.
- **Don't invent group names.** Ask if ambiguous.
- **Don't dump JSON to the user.** Render per-peer prose.
- **Don't try to remove a group.** `add_groups` is additive only; subtraction isn't supported in v0.4 (un-sharing is non-trivial in distributed settings — peers already have it).

## Mis-routing cost

Now that routing is explicit, mis-routing is mostly user-caught:

- Mis-naming a group → push lands in wrong audience (or fails forbidden). User notices immediately from the per-peer summary.
- Defaulting to `personal` when user wanted to share → no harm; user follows up with *"share those with X"*.
- Adding the wrong group via `add_groups` → can't easily un-share, but the per-peer confirmation surfaces it on the next push.

When in doubt about group names, ask.
BEA764C561E7_EOF
```

---

## Step 10 — Smoke test

```bash
# Server reachable?
~/.local/share/mem-fusion/venv/bin/python -c "
import sys, asyncio
sys.path.insert(0, '$HOME/.local/share/mem-fusion')
import core
async def main():
    stats = await core.memory_stats({})
    print(f'Total memories: {stats[\"total_memories\"]}')
    print(f'Last stored:    {stats[\"last_stored\"]}')
asyncio.run(main())
"
```

The total should match what Step 1 reported. If it doesn't, stop — something is off with the Qdrant connection or the collection name.

---

## Step 11 — Update your `~/CLAUDE.md`

Replace any cowork-memory-related section with this v0.4 snippet:

```markdown
## Vector Memory System

Connected to a local `mem-fusion` MCP server (Qdrant + nomic-embed-text on localhost).
**Check this at session start** by calling `memory_stats()` to confirm the system is live.

### Group routing — every memory has a `groups` tag

Every stored memory carries a `groups: list[str]` tag — the routing key for sharing.
Default is `["personal"]` (local-only, never leaves this machine). Never auto-classify
content to a group; the user explicitly names the audience or the default holds.

- **Hooks and silent stores**: always use `groups=["personal"]`.
- **`/remember` without an audience clause**: `groups=["personal"]`.
- **`/remember ... for <group>` (or "share this with <group>")**: tag with that group
  at store time, then `group_push(group=<group>, memory_ids=[id])`.

### When to search
- **Session start**: `search_memory(query="<current task or project name>", top_k=8)`
- **Before architectural decisions**: search for prior decisions on the same topic
- **When hitting a recurring error**: search for prior resolutions

### When to store
- **Decision made**: `store_memory(content, type="decision", importance=4, project="<name>")`
- **Novel error resolved**: `store_memory(..., type="error", importance=3)`
- **User reveals a preference**: `store_memory(..., type="preference", importance=4)`
- **User explicitly asks to remember**: use `/remember` skill → `importance=5`

### Memory types (decorative; doesn't gate sharing)
`decision` · `fact` · `preference` · `error` · `code` · `context` · `session`

### Available tools
`store_memory` · `search_memory` · `search_recent` · `upsert_memory` ·
`find_or_create` · `delete_memory` · `get_related` · `memory_stats` ·
`export_record` · `add_groups`

`add_groups(memory_ids, groups)` retroactively widens a memory's group set —
use for the store-now-share-later flow (user says "share those with X"
after the fact). Additive only; un-sharing isn't supported.

### Verification rule
Memories are point-in-time observations. Before recommending a file / function / flag
named in a memory, verify it still exists in the current code.
```

---

## Step 12 — (Optional) Install Constellation

To enable group memory across machines and teammates, paste `INSTALL_CONSTELLATION.md` into Claude Code as a separate setup. It picks up the existing mem-fusion install automatically.

---

## After upgrade — final report to the user

When Steps 1–11 pass, tell the user:

> Upgrade complete. cowork-memory → mem-fusion v0.4 done; your $COUNT memories were preserved (now tagged `groups=["personal"]`). The cowork-memory install is still on disk as rollback safety — see "Long-term cleanup" below for removing it once you're confident. **Restart your Claude Code session** so the new MCP and hooks take effect. To add group memory across machines, paste `INSTALL_CONSTELLATION.md` next.

---

## Rollback

If anything went wrong and you want to restore cowork-memory:

```bash
# 1. Restore the settings.json backup made in pre-flight
ls -t ~/.claude/settings.json.bak.* | head -1 | xargs -I {} cp {} ~/.claude/settings.json

# 2. Swap MCP back
claude mcp remove mem-fusion 2>/dev/null || true
claude mcp add cowork-memory \
  ~/.local/share/cowork-memory/venv/bin/python \
  ~/.local/share/cowork-memory/mcp_server.py

# 3. (Optional) Remove the mem-fusion install
rm -rf ~/.local/share/mem-fusion
rm -rf ~/.claude/skills/remember
```

The Qdrant data is untouched throughout — rollback restores the MCP + hooks but leaves your memories intact.

---

## Long-term cleanup (run weeks later, only after mem-fusion is stable)

The Qdrant data directory lives inside the cowork-memory install (`~/.local/share/cowork-memory/qdrant-data/`). To fully decommission cowork-memory you need to either move the data, or just leave the dir in place permanently as the data home. The simpler path:

**Option A — leave cowork-memory dir in place permanently.** Disk cost ≈ 200 MB. The dir contains code that's no longer used but no harm done. Set-and-forget.

**Option B — move the data and uninstall cowork-memory.**
```bash
# Stop the cowork-memory Qdrant launchd
launchctl unload ~/Library/LaunchAgents/com.cowork.qdrant.plist

# Move the data
mv ~/.local/share/cowork-memory/qdrant-data ~/.local/share/mem-fusion/qdrant-data

# Update the cowork plist's storage path to point at mem-fusion's location
#   (or install mem-fusion's own plist via INSTALL_MEM_FUSION.md Step 9)
# Then load + verify
launchctl load ~/Library/LaunchAgents/com.cowork.qdrant.plist
curl http://127.0.0.1:6333/collections/cowork_memories

# Once verified, delete the rest of cowork-memory
rm -rf ~/.local/share/cowork-memory
```

Option B is reversible only via your most recent Qdrant snapshot — verify the data move worked before deleting.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Step 7 says `Failed to connect` for mem-fusion | Run `~/.local/share/mem-fusion/venv/bin/python ~/.local/share/mem-fusion/mem_fusion.py` to surface import errors. Check `~/.local/share/mem-fusion/logs/`. |
| Step 8 verification still shows cowork-memory paths | `unwire_cowork_memory.py` requires `~/.claude/settings.json` to be valid JSON. Restore the backup and re-run. |
| Step 10 reports 0 memories | Qdrant is reachable but the `cowork_memories` collection is empty or named differently. `curl http://127.0.0.1:6333/collections` to inspect. |
| Migration reports `migrated=0` on first run | Either the entries already have `groups` populated (no-op), or no entries exist. Step 1 should have caught the latter. |
| Hooks double-fire after restart | `unwire_cowork_memory.py` didn't remove old entries cleanly. Manually edit `~/.claude/settings.json` to remove any block whose command contains `/.local/share/cowork-memory/`. |
