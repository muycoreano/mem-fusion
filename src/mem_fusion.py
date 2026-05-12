#!/usr/bin/env python3
"""
Mem-Fusion MCP Server — stdio transport for Claude Code.

Thin proxy over `core.py`. Registers 9 memory tools with the MCP server
and delegates each one to the corresponding `core` function. No business
logic lives in this file; it exists to translate between MCP's call/response
shape and `core`'s plain-Python function shape.
"""
import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

import core


log = core.configure_logging("mem-fusion")

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
        Tool(name="export_record",
             description=("Return a stored memory's full Qdrant record by ID, including its "
                          "768-dim vector. Used by Constellation and other extensions that need "
                          "faithful memory propagation across machines (vector copied verbatim, "
                          "not re-embedded)."),
             inputSchema={"type": "object", "properties": {
                 "id": {"type": "string", "description": "Memory ID from a prior store/search result"},
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
    raise ValueError(f"Unknown tool: {name}")


async def main():
    log.info("Mem-Fusion MCP Server starting")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
