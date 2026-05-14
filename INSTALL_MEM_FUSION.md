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
- **Python 3.12 MCP server** — exposes 10 memory tools to Claude Code via stdio (plus 2 group tools when Constellation is also installed)
- **4 hooks** — SessionStart, UserPromptSubmit, Stop, PostToolUse:Write (all auto-tagged with `groups=["personal"]`)
- **`/remember` skill** — explicit group-keyed tagging (default scope is `personal`, local-only)

Everything is managed by `launchd`. Disk footprint ≈ 200 MB.

---

## Pre-flight — verify before starting

```bash
# 1. macOS only
[[ "$(uname)" == "Darwin" ]] || { echo "This installer is macOS-only"; exit 1; }

# 2. Architecture detection
ARCH="$(uname -m)"   # arm64 (Apple Silicon) or x86_64 (Intel)
echo "Arch: $ARCH"

# 3. Homebrew present + correct prefix for this arch
if [[ "$ARCH" == "arm64" ]]; then BREW_PREFIX="/opt/homebrew"; else BREW_PREFIX="/usr/local"; fi
[[ -x "$BREW_PREFIX/bin/brew" ]] || { echo "Install Homebrew first: https://brew.sh"; exit 1; }

# 4. Claude CLI present
command -v claude >/dev/null || { echo "Install Claude Code first"; exit 1; }

# 5. Nothing already on the mem-fusion ports
lsof -nP -iTCP:6333  -sTCP:LISTEN -t >/dev/null 2>&1 && echo "WARN: port 6333 already in use"
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
ollama --version    # 0.20+
python3.12 --version
```

If `python@3.12` is unavailable, fall back to `python@3.11` and adjust Step 3.

---

## Step 2 — Lay out the install directory

```bash
mkdir -p ~/.local/share/mem-fusion/{bin,scripts,logs,queue,qdrant-data,snapshots}
```

---

## Step 3 — Python venv + dependencies

```bash
python3.12 -m venv ~/.local/share/mem-fusion/venv
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

## Step 4 — Download the Qdrant binary

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
~/.local/share/mem-fusion/bin/qdrant --version
```

---

## Step 5 — Install the Qdrant config

```bash
cat > ~/.local/share/mem-fusion/qdrant-config.yaml <<'8DD30E144A92_EOF'
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
8DD30E144A92_EOF
```

---

## Step 6 — Install the MCP server

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

## Step 7 — Install the collection initializer

```bash
cat > ~/.local/share/mem-fusion/scripts/init_collection.py <<'D9B604D06D66_EOF'
#!/usr/bin/env python3
"""
Initialize the cowork-memories Qdrant collection for a Mem-Fusion install.

  QDRANT_URL    default: http://127.0.0.1:6333

Safe to re-run — skips creation if the collection already exists; creates
payload indexes idempotently.
"""
import os
import sys
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PayloadSchemaType

QDRANT_URL  = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION  = "cowork_memories"
VECTOR_SIZE = 768  # nomic-embed-text dimensions

print(f"→ qdrant: {QDRANT_URL}")
print(f"→ collection: {COLLECTION}")

client = QdrantClient(url=QDRANT_URL, timeout=10)

existing = [c.name for c in client.get_collections().collections]
if COLLECTION in existing:
    print(f"Collection '{COLLECTION}' already exists — skipping creation.")
else:
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    print(f"Created collection '{COLLECTION}'.")

indexes = {
    "type":         PayloadSchemaType.KEYWORD,
    "project":      PayloadSchemaType.KEYWORD,
    "groups":       PayloadSchemaType.KEYWORD,  # v0.4 list-valued routing key
    "source":       PayloadSchemaType.KEYWORD,  # legacy v0.3, kept for migration
    "group_name":   PayloadSchemaType.KEYWORD,  # legacy v0.3, kept for migration
    "session_id":   PayloadSchemaType.KEYWORD,
    "content_hash": PayloadSchemaType.KEYWORD,
    "origin_node":  PayloadSchemaType.KEYWORD,
    "tags":         PayloadSchemaType.KEYWORD,
    "importance":   PayloadSchemaType.INTEGER,
    "timestamp":    PayloadSchemaType.DATETIME,
    "submitted_at": PayloadSchemaType.DATETIME,
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
D9B604D06D66_EOF
chmod +x ~/.local/share/mem-fusion/scripts/init_collection.py
```

---

## Step 8 — Install the hook scripts

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

## Step 9 — Install the launchd plists

Substitute `$HOME` (and `$OLLAMA_BIN` for Ollama) at write time:

```bash
if [[ "$(uname -m)" == "arm64" ]]; then OLLAMA_BIN="/opt/homebrew/bin/ollama"; else OLLAMA_BIN="/usr/local/bin/ollama"; fi
export HOME_LIT="$HOME"
export OLLAMA_BIN
```

```bash
cat > ~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist <<7E47CA3D1B16_EOF
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
7E47CA3D1B16_EOF
```

```bash
cat > ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist <<7438C490D56E_EOF
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
7438C490D56E_EOF
```

---

## Step 10 — Start services + pull embedding model + init collection

```bash
launchctl load ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist
launchctl load ~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist

curl -s --retry 12 --retry-delay 1 --retry-connrefused http://127.0.0.1:11434/api/tags >/dev/null && echo "  Ollama up"
curl -s --retry 12 --retry-delay 1 --retry-connrefused http://127.0.0.1:6333/healthz   >/dev/null && echo "  Qdrant up"

ollama pull nomic-embed-text

~/.local/share/mem-fusion/venv/bin/python \
  ~/.local/share/mem-fusion/scripts/init_collection.py
```

---

## Step 11 — Register the MCP with Claude Code

```bash
claude mcp add --scope user mem-fusion \
  ~/.local/share/mem-fusion/venv/bin/python \
  ~/.local/share/mem-fusion/mem_fusion.py

claude mcp list | grep mem-fusion
```

You should see `mem-fusion: ... - ✓ Connected`.

---

## Step 12 — Wire the 4 hooks into `~/.claude/settings.json`

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

---

## Step 13 — Install the `/remember` skill

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

## Step 14 — Smoke test

```bash
cat > ~/.local/share/mem-fusion/scripts/smoke-test.sh <<'7760D727B68B_EOF'
#!/usr/bin/env bash
# Defaults match a standard install; tests override these to point at a
# tempdir-scoped Qdrant. core.py inside the heredoc honors QDRANT_URL/OLLAMA_URL
# the same way (module-level os.getenv with the same defaults).
set -e
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"

launchctl list | grep -E "com.branchapp.memfusion" | head
curl -s "$QDRANT_URL/healthz"
curl -s "$OLLAMA_URL/api/tags" | python3 -c "import sys,json; print('Ollama models:', [m['name'] for m in json.load(sys.stdin)['models']])"

# Best-effort MCP-registration check. Skipped in environments where the
# claude CLI isn't installed (CI, tests); demoted to a warning if mem-fusion
# isn't yet registered (which is the case during partial installs).
if command -v claude >/dev/null 2>&1; then
    claude mcp list | grep mem-fusion || echo "WARN: mem-fusion not registered with claude CLI"
else
    echo "WARN: claude CLI not found; skipping MCP registration check"
fi

~/.local/share/mem-fusion/venv/bin/python - <<'PYEOF'
import asyncio, sys
sys.path.insert(0, str(__import__('pathlib').Path.home() / ".local/share/mem-fusion"))
import core

async def main():
    r1 = await core.store_memory({
        "content": "Smoke test: Mem-Fusion install completed on " + __import__('datetime').datetime.now().isoformat(),
        "type": "context", "importance": 3, "project": "install-test",
        "tags": ["smoke-test"], "groups": ["personal"],
    })
    print("STORE:", r1)

    r2 = await core.search_memory({"query": "smoke test install", "top_k": 3})
    print("SEARCH count:", r2["count"])
    assert r2["count"] >= 1, "Search didn't find the just-stored memory"

    r3 = await core.memory_stats({})
    print("STATS:", r3)

    print("\n✓ All smoke tests passed")

asyncio.run(main())
PYEOF
7760D727B68B_EOF
chmod +x ~/.local/share/mem-fusion/scripts/smoke-test.sh
```

```bash
~/.local/share/mem-fusion/scripts/smoke-test.sh
```

Expect `✓ All smoke tests passed`.

---

## Step 15 — Tell the user what to add to their CLAUDE.md

Print this snippet and instruct the user to paste it into `~/CLAUDE.md` (or a project-level `CLAUDE.md`):

```markdown
## Vector Memory System

Connected to a local `mem-fusion` MCP server (Qdrant + nomic-embed-text on localhost).
**Check this at session start** by calling `memory_stats()` to confirm the system is live.

### Group routing — every memory has a `groups` tag

Every stored memory carries a `groups: list[str]` tag — the routing key for sharing.
Default is `["personal"]` (local-only, never leaves this machine). Never auto-classify
content to a group; the user explicitly names the audience or the default holds.

- **Hooks and silent stores**: always use `groups=["personal"]`. Hook-captured memories
  describe this peer's work and should never propagate without explicit user intent.
- **`/remember` without an audience clause**: `groups=["personal"]`.
- **`/remember ... for <group>` (or "share this with <group>")**: tag with that group
  at store time, then `group_push(group=<group>, memory_ids=[id])`.

### When to search
- **Session start**: `search_memory(query="<current task or project name>", top_k=8)`
- **Before architectural decisions**: search for prior decisions on the same topic
- **When hitting a recurring error**: search for prior resolutions
- **When unsure about a user preference**: search `type="preference"`

### When to store
- **Decision made**: `store_memory(content, type="decision", importance=4, project="<name>")`
- **Novel error resolved**: `store_memory(..., type="error", importance=3)`
- **User reveals a preference**: `store_memory(..., type="preference", importance=4)`
- **Important context learned**: `store_memory(..., type="context", importance=3)`
- **User explicitly asks to remember**: use `/remember` skill → `importance=5`

### What NOT to store
Trivial facts, transient state, things derivable from code or `git log`.

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

## Troubleshooting

| Symptom | Fix |
|---|---|
| `claude mcp list` shows mem-fusion but `Failed to connect` | Run the server directly to surface import errors: `~/.local/share/mem-fusion/venv/bin/python ~/.local/share/mem-fusion/mem_fusion.py` (will hang waiting for stdio — Ctrl-C). Check `~/.local/share/mem-fusion/logs/mcp.log`. |
| Qdrant won't start | Confirm `MALLOC_CONF=background_thread:false` is in the plist `EnvironmentVariables` and `NumberOfFiles=65536` is set. Check `~/.local/share/mem-fusion/logs/qdrant-error.log`. |
| Search returns 0 results | Verify Ollama is up: `curl http://127.0.0.1:11434/api/tags`. Verify the model is pulled: `ollama list \| grep nomic-embed-text`. |
| Hooks not firing | Confirm `~/.claude/settings.json` was merged correctly (Step 12). Run scripts manually to surface errors: `~/.local/share/mem-fusion/scripts/session_prime.sh`. |
| Session ingestion logs "no session file found" | Confirm `~/.claude/sessions/*.jsonl` exists. If Claude Code moved the session path, edit `SESSIONS_DIR` in `ingest_session.py`. |

Logs directory: `~/.local/share/mem-fusion/logs/` — `mcp.log`, `qdrant.log`, `qdrant-error.log`, `ollama.log`, `ingest.log`.

---

## Uninstall

```bash
claude mcp remove mem-fusion

launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist 2>/dev/null
launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist 2>/dev/null
rm ~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist \
   ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist

echo "Edit ~/.claude/settings.json — remove the 4 mem-fusion entries under .hooks, or restore from the pre-flight .bak."

read -p "Delete all stored memories? [y/N] " yn
[[ "$yn" == "y" ]] && rm -rf ~/.local/share/mem-fusion

rm -rf ~/.claude/skills/remember
```

---

## After install — final report to the user

When all 15 steps + smoke test pass, tell the user:

> Your Mem-Fusion vector memory system is live. **Restart your Claude Code session** for the hooks to take effect. Then paste the CLAUDE.md snippet from Step 15 into your `~/CLAUDE.md`. Try it out by saying *"remember that I prefer Python virtual envs created with `uv`"* — Claude should call the `/remember` skill and confirm with a memory ID.
