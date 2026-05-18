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
        Tool(name="add_connector_ids",
             description=("Additively tag one or more existing memories with v0.5 connector IDs. "
                          "Use for the store-now-share-later workflow under the v0.5 connector "
                          "model: store memories (default connector_ids=[], local-only), then "
                          "later add a connector id before calling `/remember push <connector-id>`. "
                          "Never removes a connector_id — only adds. Same shape as add_groups; "
                          "returns {updated, no_op, errors} telemetry. Render a brief summary."),
             inputSchema={"type": "object", "properties": {
                 "memory_ids":    {"type": "array", "items": {"type": "string"},
                                   "description": "IDs from prior store_memory / search results"},
                 "connector_ids": {"type": "array", "items": {"type": "string"},
                                   "description": "Connector IDs to add (additive union); "
                                                  "must match an id in ~/.local/share/mem-fusion/connector.json"},
             }, "required": ["memory_ids", "connector_ids"]}),
        Tool(name="load_connectors_config",
             description=("Load and validate ~/.local/share/mem-fusion/connector.json. "
                          "Returns {connectors: [...], errors: [...], warnings: [...], config_path}. "
                          "Call FIRST in any /remember push|pull <connector-id> orchestration; "
                          "if errors is non-empty, surface them to the user and abort — do not "
                          "attempt to push/pull with an invalid config. Each connector entry has "
                          "{id, type, ...type-specific-fields}; v0.5 supports type=slack with a "
                          "`channel` field (Slack channel id like C0XXXXXXX or channel name; "
                          "if name, resolve via slack_search_channels at orchestration time)."),
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string",
                          "description": "Optional override path (tests only)."},
             }, "required": []}),
        Tool(name="build_connector_envelope",
             description=("Compose a connector-substrate wire body from a local memory + "
                          "connector_id. Powers /remember push <connector-id> orchestration. "
                          "Combines load_connectors_config + export_record + connector ABC "
                          "(build_envelope_from_record + format_envelope) into one call. "
                          "Returns {body, channel, connector_id, type, submitted_at, envelope, "
                          "body_size} on success — pass `body` as the `message` arg to "
                          "slack_send_message; `channel` is the connector.json channel value "
                          "(may need slack_search_channels resolution if it's a name not an id). "
                          "Use submitted_at to advance the connector cursor on success via "
                          "set_connector_cursor. Errors include connector_config_invalid, "
                          "connector_not_found, memory_not_found, build_envelope_failed, and "
                          "body_too_large_for_substrate."),
             inputSchema={"type": "object", "properties": {
                 "memory_id":    {"type": "string",
                                  "description": "Local memory id (from store/search/export)."},
                 "connector_id": {"type": "string",
                                  "description": "Connector entry's id field in connector.json."},
             }, "required": ["memory_id", "connector_id"]}),
        Tool(name="get_connector_cursor",
             description=("Read the persisted cursor (ISO timestamp) for a connector — the "
                          "submitted_at of the latest entry successfully pushed/pulled. Returns "
                          "{connector_id, cursor: str | null}. null means no cursor yet (first "
                          "push/pull). Used by /remember push to filter local memories whose "
                          "submitted_at > cursor, and by /remember pull as the substrate query "
                          "lower bound."),
             inputSchema={"type": "object", "properties": {
                 "connector_id": {"type": "string"},
             }, "required": ["connector_id"]}),
        Tool(name="set_connector_cursor",
             description=("Persist a cursor (ISO-8601 timestamp string) for a connector. "
                          "Atomic write to ~/.local/share/mem-fusion/connector_cursors.json. "
                          "Called by /remember push|pull after each successful operation. "
                          "Idempotent. Returns {status: 'ok', connector_id, cursor}."),
             inputSchema={"type": "object", "properties": {
                 "connector_id": {"type": "string"},
                 "iso_ts":       {"type": "string",
                                  "description": "ISO-8601 timestamp (typically the memory's submitted_at)."},
             }, "required": ["connector_id", "iso_ts"]}),
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
    if name == "add_groups":         return await core.add_groups(args)
    if name == "add_connector_ids":  return await core.add_connector_ids(args)
    if name == "load_connectors_config":   return await core.load_connectors_config_tool(args)
    if name == "build_connector_envelope": return await core.build_connector_envelope(args)
    if name == "get_connector_cursor":     return await core.get_connector_cursor_tool(args)
    if name == "set_connector_cursor":     return await core.set_connector_cursor_tool(args)
    if name == "group_pull":         return await group_pull(args)
    if name == "group_push":         return await group_push(args)
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
