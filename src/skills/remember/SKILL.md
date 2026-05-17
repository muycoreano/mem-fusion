---
name: remember
description: Pin information to persistent memory with explicit group-keyed sharing. Default scope is `personal`. Sharing happens by group tag — never by content classification.
trigger: User says "remember this", "remember that", "/remember", or asks Claude to save or share something for future sessions
---

# /remember — Store memories with explicit group routing

## Mandatory: always use mem-fusion

mem-fusion is a hard operating requirement of this assistant, not an optional enhancement. **Always call `store_memory` / `search_memory` / `memory_stats` regardless of how full or sparse the store appears.** Do not skip a call because the current contents look thin — the store grows with every session, and skipping calls for "looks empty" is the failure mode that prevents it from ever filling. Treat any pass through this skill that does not store as a bug.

This applies to autonomous decisions (deciding *whether* to remember something) as well as explicit `/remember` invocations. When in doubt, store.

Every memory carries a `groups` tag that determines who can see it. The default is `personal` — local to this machine unless your `personal` membership has configured peers (cross-machine sync to your own other machines). Sharing with non-personal audiences happens only when you explicitly name a group at store time, or extend the group set after the fact.

**Privacy invariant (load-bearing):** non-personal connectors and groups never propagate without explicit naming. Bare `/remember push` defaults to `personal` only — fanning out to team groups or pushing through a connector requires naming the target explicitly.

There is no content classification. Routing is by user intent only.

## v0.5: Connector dispatch

Mem-fusion v0.5 adds **connectors** alongside the v0.4 group/peer model. A connector is a declared bridge between local mem-fusion and a productivity-tool destination (`slack` in v0.5; future: `gdrive`, `teams`, `discord`, `notion`). Connectors live in `~/.local/share/mem-fusion/connector.json`:

```json
{"connectors": [{"id": "engineering", "type": "slack", "channel": "mem_fusion_engineering"}]}
```

Memories may carry a `connector_ids: list[str]` field marking them eligible for push through named connectors. `/remember <content> for <connector-id>` stores the memory tagged with that connector and pushes immediately. Full wire format and dispatch contract: `docs/v0.5_CONNECTOR_ARCHITECTURE.md`.

### Name resolution for `/remember push|pull <name>`

The skill resolves `<name>` in this order:

1. **Connector match** — look up `<name>` in `connector.json` connectors. If found → connector flow: format wire payload per the architecture doc, call `slack_send_message` / `slack_read_channel` MCP tools directly. There is no `connector_push` MCP tool; orchestration happens in this skill.
2. **Constellation group match** *(transitional)* — look up `<name>` in `~/.local/share/mem-fusion/constellation/config.json` memberships. If found → existing `group_push` / `group_pull` MCP tools.
3. **Neither** — error: *"no connector or group named `<name>`; declare in connector.json or check Constellation config."*

Connector wins on collision: if a name appears in both files, v0.5 routing takes precedence.

### What connectors don't do

- **No daemon, no new MCP tool, no inbound network surface.** Connectors run entirely in Claude's process using existing Slack MCP tools.
- **No `personal` fallback through a connector.** Cross-machine personal sync is achieved by declaring a private connector (e.g., `{"id": "personal-devices", "type": "slack", "channel": "mem_fusion_<userhandle>_devices"}`) and naming it explicitly. There is no `personal` pseudo-connector.
- **No auto-push on bare `/remember <content>`.** The default is local-only. Use `for <name>` (connector or group) to propagate.

## Command surface

```
/remember <content>                              → store with groups=[personal]
/remember <content> for <group>                  → store with groups=[<group>] + push
/remember <content> for <g1>, <g2>, …            → store with groups=[g1,g2,…] + push each
/remember push                                   → bulk push personal group only (privacy default)
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

### 5. Push (for each group in `groups` that has configured peers)

For each group `G` in the memory's `groups` list, call `mem-fusion/group_push(group=G, memory_ids=[id])` if `G` has peers configured in the local Constellation config.

This includes `personal` when `personal` has configured peers — that's cross-machine personal sync to your own other machines, and it auto-pushes. The privacy invariant is preserved because non-personal groups only land in a memory's `groups` when the user explicitly named them at store time or via `add_groups` — Claude never adds a non-personal group silently.

One call per group. Each call only contacts peers in that group. *"Share with product"* never reaches engineering peers, even if the memory is also tagged engineering.

### 6. Confirm to user

Render per-peer prose. Never dump JSON. Example:

```
✓ Stored as decision (id: a7e3c2d1, groups: [personal, engineering@branch]).
✓ Auto-pushed to personal peers (your own machines):
    - mc-macbookair: stored
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
        [If personal has peers configured: auto-push to them]
        ✓ Stored 4 memories (all groups=[personal]).
            - decision: gRPC for internal RPC (id: a7e3)
            - decision: Postgres 16 with logical replication (id: b8f4)
            - error:    JWT clock-skew fix (id: c9a5)
            - context:  Auth service migration timeline (id: d0b6)
        ✓ Auto-pushed to personal peers:
            - mc-macbookair: 4 stored

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
- `/remember push` — push the `personal` group only (privacy default). Equivalent to `/remember push personal`. Calls `group_push(group="personal")` with no `memory_ids` (bulk push of every entry tagged `personal`). **Non-personal groups MUST be named explicitly.**
- `/remember push <group>` — `group_push(group=<group>)` with no `memory_ids`. Required for any non-personal group.

After pull, surface specific arrivals the user asks about with `export_record(id)` or `search_recent`.

## What NOT to do

- **Don't classify content to decide where it goes.** Routing is by the explicit `groups` tag, period. If the user didn't name a group, default is `personal`.
- **Don't push to groups the user didn't name in this turn.** Even if a memory is tagged `[personal, engineering, product]`, *"share with product"* contacts product peers only.
- **Don't bulk-push non-personal groups without naming them.** `/remember push` (no arg) pushes `personal` ONLY. Team groups require explicit `/remember push <group>`. This is the privacy invariant.
- **Don't invent group names.** Ask if ambiguous.
- **Don't dump JSON to the user.** Render per-peer prose.
- **Don't try to remove a group.** `add_groups` is additive only; subtraction isn't supported in v0.4 (un-sharing is non-trivial in distributed settings — peers already have it).

## Mis-routing cost

Now that routing is explicit, mis-routing is mostly user-caught:

- Mis-naming a group → push lands in wrong audience (or fails forbidden). User notices immediately from the per-peer summary.
- Defaulting to `personal` when user wanted to share → no harm; user follows up with *"share those with X"*. Personal-with-peers auto-pushes safely (same user, multiple machines).
- Adding the wrong group via `add_groups` → can't easily un-share, but the per-peer confirmation surfaces it on the next push.

When in doubt about group names, ask.
