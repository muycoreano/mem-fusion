---
name: remember
description: Pin information to persistent memory. Routes behavioral rules and personal preferences to file-based memory (always loaded, never shared); routes knowledge and context to the vector DB (semantically searchable, optionally shared via Constellation).
trigger: User says "remember this", "remember that", "/remember", or asks Claude to save something for future sessions
---

# /remember — Route to the right memory layer

Two memory stores serve different purposes. `/remember` decides which one the content belongs in and writes it there.

| Destination | What goes there | Loaded when | Shared with group? |
|---|---|---|---|
| **File-based memory** (`~/.claude/projects/<encoded-cwd>/memory/`) | Behavioral rules, preferences, conventions, meta-instructions about how Claude should act | Always at session start | **Never** |
| **Vector DB** (`mem-fusion/store_memory`) | Knowledge, decisions, facts, errors, code patterns, contextual observations | On-demand semantic search | **Yes** if Constellation is installed |

The destination matters for both relevance *and* privacy — behavioral rules are personal and shouldn't propagate to teammates' machines.

## Protocol

### 1. Identify what to remember

- If the user specified content explicitly, use that verbatim.
- If the user said "remember this" without specifying content, summarize the current conversation context into a clear, self-contained statement.

### 2. Classify the content

**Behavioral rule** — content tells Claude *how to act going forward*. Signals:
- Starts with "always", "never", "I prefer", "make sure", "don't", "should/shouldn't"
- Is a personal preference, convention, style choice, or workflow rule
- Examples: *"I prefer concise responses"*, *"always run tests before commits"*, *"use uv for Python environments"*, *"never push directly to main"*

**Knowledge** — content describes *facts, decisions, events, or observations*. Signals:
- Records a project decision, architectural choice, or trade-off
- Captures an error and its fix, or a stable code pattern
- Notes a fact about the system, codebase, or environment
- Examples: *"we picked Postgres 16 for the auth service"*, *"the cache invalidation bug was caused by stale TTLs"*, *"the new SSO endpoint is /v2/sso/init"*

**Ambiguous** — if it could plausibly be either, ask the user:

> *"Is this a behavioral rule for how I should act (stays local, never shared) — or knowledge to remember and share with your group?"*

Don't guess. The cost of asking is low; the cost of mis-routing a personal preference into the vector DB (which then auto-pushes to teammates) is a privacy leak.

### 3a. Behavioral rule → file-based memory

Write a new memory file in your auto-memory directory (the same directory Claude Code loaded `MEMORY.md` from at session start — typically `~/.claude/projects/<encoded-cwd>/memory/`).

**File:** `preference_<kebab-case-slug>.md` in that directory.

**Frontmatter + body:**
```markdown
---
name: <short human-readable name>
description: <one-line summary of when this rule applies>
type: preference
---
**Rule:** <the full rule, verbatim or lightly polished>

**Why:** <reason if user gave one; omit if not>

**How to apply:** <when/where this rule kicks in>
```

**Index:** Add a one-line pointer to the same directory's `MEMORY.md` under an appropriate section (create a `## User preferences` section if none fits):

```markdown
- [<name>](preference_<slug>.md) — <description>
```

**Confirm to user:**
> ✓ Pinned as a behavioral rule in `preference_<slug>.md`.
> Loads at every session start. Stays local, never shared with the group.

### 3b. Knowledge → vector DB

Classify the memory type from this set: `decision`, `fact`, `error`, `code`, `context`. (Don't use `preference` — that's routed to file-based above. Don't use `session` — that's reserved for the Stop hook.)

Call `mem-fusion/store_memory` with:
- `content`: the content
- `type`: classified above
- `importance: 5` (always for `/remember`)
- `project`: inferred from current conversation context
- `tags`: inferred from content topic

If Constellation is installed, also call `mem-fusion/group_push(id=<returned id>)` to share with the group. (If Constellation isn't installed, `group_push` returns `{"error": "constellation_not_installed"}` — that's expected, not an error worth reporting.)

**Confirm to user:**
```
✓ Stored as <type> (id: <short id>, importance: 5, project: <project>).
✓ Shared with <group_name>:
    - <peer>: stored | duplicate
    - <peer>: unreachable (<reason>)
```

If Constellation isn't installed, replace the "Shared with" lines with:
> Local only — Constellation not installed.

## Examples

**Behavioral rule:**
```
User:   /remember always use uv for Python environments on this machine

Claude: ✓ Pinned as a behavioral rule in preference_use-uv-for-python.md.
        Loads at every session start. Stays local, never shared.
```

**Knowledge:**
```
User:   /remember we picked Postgres 16 with logical replication for the auth service

Claude: ✓ Stored as decision (id: a7e3c2d1, importance: 5, project: auth-service).
        ✓ Shared with engineering@branch:
            - alice-desktop: stored
            - bob-mac:       stored
            - carol-laptop:  unreachable (connection refused)
```

**Ambiguous → ask:**
```
User:   /remember the deploy script uses Python 3.12

Claude: Quick check — is this a behavioral rule (always use Python 3.12
        for the deploy script going forward, stays local) or knowledge
        (a fact about the current deploy script, shared with your group)?
```

## Mis-routing is costly in one direction, cheap in the other

- Mis-routing a **behavioral rule** into the vector DB → it auto-pushes to every teammate's machine. Privacy leak.
- Mis-routing **knowledge** into file-based memory → teammates won't see it. Annoying but recoverable; the user can `/remember` it again as knowledge.

When in doubt, ask.
