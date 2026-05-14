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
