## Vector Memory System

You have a local `mem-fusion` MCP server (Qdrant + nomic-embed-text on localhost) — your persistent semantic memory across sessions.

**Always use mem-fusion, regardless of how full or sparse it appears.** Do not judge its usefulness from a single search returning few results. The store grows with every session; treating it as "empty therefore not useful" is the failure mode that prevents it from ever filling. Skipping a mem-fusion call when the situation calls for one is a bug, not a judgment call.

### Mandatory operations

- **Session start**: call `memory_stats()` first, then `search_memory(query="<current task or project name>", top_k=8)`. Always — even when the task seems trivial or the user just gave a one-liner.
- **Before architectural decisions**: `search_memory(query="<topic>")` for prior decisions on the same topic.
- **When hitting a recurring error**: `search_memory(query="<error symptom>", type="error")`.
- **Decision made**: `store_memory(content, type="decision", importance=4, project="<name>")`.
- **Novel error resolved**: `store_memory(..., type="error", importance=3)`.
- **User reveals a preference**: `store_memory(..., type="preference", importance=4)`.
- **Important context learned**: `store_memory(..., type="context", importance=3)`.
- **User explicitly asks to remember**: use the `/remember` skill → `importance=5`.

### Group routing — every memory has a `groups` tag

Every stored memory carries a `groups: list[str]` tag — the routing key for sharing.
Default is `["personal"]` (local-only, never leaves this machine). Never auto-classify
content to a group; the user explicitly names the audience or the default holds.

- **Hooks and silent stores**: always use `groups=["personal"]`. Hook-captured memories
  describe this peer's work and should never propagate without explicit user intent.
- **`/remember` without an audience clause**: `groups=["personal"]`.
- **`/remember ... for <group>` (or "share this with <group>")**: tag with that group
  at store time, then `group_push(group=<group>, memory_ids=[id])`.

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
