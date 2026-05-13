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
        Tool(name="export_record",
             description=("Return a stored memory's full Qdrant record by ID, including its "
                          "768-dim vector. Used by Constellation and other extensions that need "
                          "faithful memory propagation across machines (vector copied verbatim, "
                          "not re-embedded)."),
             inputSchema={"type": "object", "properties": {
                 "id": {"type": "string", "description": "Memory ID from a prior store/search result"},
             }, "required": ["id"]}),
        Tool(name="group_pull",
             description=("Pull new memories from every peer in this node's group via the "
                          "local Constellation daemon. Returns per-peer telemetry "
                          "{peers: [{node_name, group_name, status: responsive|unreachable, "
                          "entry_ids?, reason?}]}. After calling this, use export_record(id) "
                          "or search_recent to surface the new content to the user — render "
                          "a per-peer natural-language summary; never dump the raw JSON. "
                          "Requires Constellation to be installed."),
             inputSchema={"type": "object", "properties": {}, "required": []}),
        Tool(name="group_push",
             description=("Share a locally-stored memory with every peer in this node's group "
                          "via the local Constellation daemon. Takes the local memory's ID "
                          "(from a prior store_memory result). Returns per-peer delivery "
                          "telemetry {peers: [{node_name, group_name, status, delivery?, "
                          "reason?}]}. Render a per-peer summary to the user; never dump JSON. "
                          "Requires Constellation to be installed."),
             inputSchema={"type": "object", "properties": {
                 "id": {"type": "string",
                        "description": "Local memory ID from a prior store_memory result"},
             }, "required": ["id"]}),
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
    if name == "group_pull":     return await group_pull(args)
    if name == "group_push":     return await group_push(args)
    raise ValueError(f"Unknown tool: {name}")


# ── Group tools — thin proxies to local Constellation gateway ────────────
async def group_pull(_args):
    """POST /pull on local Constellation gateway. Returns per-peer telemetry."""
    try:
        async with httpx.AsyncClient(timeout=GATEWAY_TIMEOUT_S) as client:
            r = await client.post(f"{CONSTELLATION_GATEWAY}/pull", json={})
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


async def group_push(args):
    """POST /push on local Constellation gateway. mem-fusion fetches the full
    record via core.export_record and forwards it; the gateway handles the
    fan-out and the local-entry augmentation.
    """
    memory_id = args.get("id")
    if not memory_id:
        return {"error": "missing_argument", "detail": "id is required"}

    record = await core.export_record({"id": memory_id})
    if "error" in record:
        return {"error": "memory_not_found", "detail": record["error"]}

    try:
        async with httpx.AsyncClient(timeout=GATEWAY_TIMEOUT_S) as client:
            r = await client.post(f"{CONSTELLATION_GATEWAY}/push",
                                  json={"record": record})
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


async def main():
    log.info("Mem-Fusion MCP Server starting")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
