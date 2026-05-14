# Mem-Fusion

**Persistent memory for Claude Code — across sessions, across machines, across your team.**

Out of the box, Claude Code is brilliant in the moment and amnesic between moments. Every session starts blank. Yesterday's architectural decision, last week's stated preference, last month's hard-won bug fix — all gone. You re-explain your codebase. You re-establish your conventions. You re-debug the same errors. The smarter Claude gets as a model, the more painful the reset becomes, because what you're losing each time is more valuable.

**Mem-Fusion fixes this.** A local memory layer watches your Claude Code sessions, captures decisions and patterns as you work, and surfaces them automatically the next time they're relevant. Most of the time it's invisible — Claude finds and uses what it needs, when it needs it — but `/remember` is there when you want to pin something explicitly. The longer you use it, the sharper your Claude gets at *your* work, in *your* codebase, with *your* preferences. Everything runs on your machine; nothing leaves it.

**Constellation** is the optional companion that extends this memory across machines and teammates. When your laptop's Claude learns something useful, your desktop's Claude knows it too. When a teammate cracks a tricky problem, the rest of the team benefits. AI capability stops being trapped per-person, per-machine, per-session and starts compounding across the people working on the same thing.

---

## Install

Both installers are AI-native: paste the markdown into a Claude Code session and Claude runs the steps with your approval. Requires macOS + Homebrew + Claude Code already installed.

### 1. Personal memory (required, ~10 min)

1. Open Claude Code in a terminal.
2. Copy contents of INSTALL_MEM_FUSION.md
3. Paste into Claude Code and approve all subsequent steps

This will install Qdrant 1.13.4, Ollama + `nomic-embed-text`, the Mem-Fusion MCP server, 4 Claude Code hooks, and the `/remember` skill. ~200 MB on disk.

### 2. Shared group memory (optional, ~5 min per peer)

Install Mem-Fusion first on each machine. Then on each one:

1. Copy contents of INSTALL_CONSTELLATION.md
2. Paste into Claude Code and approve all subsequent steps

Constellation runs as a persistent HTTP MCP daemon (port 7533). Memories from the group will now be accessible automatically as if they were local.


Both INSTALL_MEM_FUSION.md and INSTALL_CONSTELLATION.md will instruct you on additions for CLAUDE.md during install.


---

## Using it day-to-day

Most memory operations are invisible — the four Claude Code hooks (`SessionStart`, `UserPromptSubmit`, `Stop`, `PostToolUse:Write`) inject relevant context before Claude responds and capture decisions, errors, preferences, and code patterns as you work. You don't have to remember to recall or store; the system does it.

`/remember` is the explicit pin — use it when you've just made a decision (or stated a preference) you want Claude to act on going forward.

### Group-keyed sharing — explicit, never inferred

Every memory carries a `groups` tag (a list of group names) that determines who can see it. The default is `personal` — local-only, never leaves your machine. Sharing happens when you explicitly name a group, either at store time or after the fact.

There is no content classification. Mem-Fusion doesn't try to guess whether a memory should be shared based on its shape — *you* tell it the audience, and the same Qdrant collection holds everything. The `groups` field is the routing key.

| Memory's groups | Where it lives | Who sees it |
|---|---|---|
| `["personal"]` (default) | This machine's Qdrant only | Only your Claude on this machine. With cross-machine personal sync configured, also your other personal-group machines. |
| `["personal", "engineering@team"]` | This machine + every engineering@team peer | Your machines + engineering teammates' machines |
| `["engineering@team"]` (no personal) | Engineering teammates only | Skips your personal-group machines if any |

### `/remember` examples

**Default (local-only)** — no group named, defaults to `personal`:

```
You:   /remember always use uv for Python environments

Claude: ✓ Stored with groups=[personal]. Stays on this machine.
```

**Explicit group at store time** — store *and* push in one call:

```
You:   /remember We standardized on PostgreSQL 16 with logical replication
       for the auth service, for engineering@branch

Claude: ✓ Stored as decision (id: a7e3c2d1, groups=[engineering@branch]).
        ✓ Pushed to engineering@branch:
            - alice-desktop: stored
            - bob-mac:       stored
            - carol-laptop:  unreachable (connection refused)
```

**Store-now-share-later** — capture during work, decide audience after:

```
You:   "Let's pause and store what we learned this morning."
Claude: ✓ Stored 4 memories with groups=[personal].

You:   "Share those with engineering."
Claude: ✓ Added engineering@branch to 4 memories.
        ✓ Pushed to engineering@branch: 4 stored on bob, 4 stored on alice.

You:   "Also share them with product."
Claude: ✓ Added product@branch to 4 memories.
        ✓ Pushed to product@branch peers ONLY: 4 stored on dave, 4 on eve.
          (Engineering peers not re-contacted — already have them.)
```

**Scope rule:** push only contacts peers in the named group, even if the memory is also tagged for other groups. *"Share with product"* never reaches engineering peers. To push to multiple groups in one user turn, name them all explicitly.

If Constellation isn't installed, the `groups` tag is still recorded; the "Pushed to..." line is replaced with "Local only — Constellation not installed." Tagged memories propagate the moment Constellation comes online and another peer pulls.

### How memories surface

You don't need to explicitly recall a memory. The `UserPromptSubmit` hook embeds your message and searches Qdrant for relevant memories above a similarity threshold (0.75 by default) — anything that matches is injected into Claude's context before it responds. The system also nudges Claude on architectural questions, recurring errors, and preference-shaped statements.

Manual recall is always available — just ask: *"What did we decide about the auth service?"* triggers Claude to call `search_memory(...)` and surface the relevant entry.

### Prompt Tip - Periodically Consolidate your Learnings

After working 2-3 hours straight with Claude, it tends to get a bit 'loose' naturally with and without mem-fusion.  Telling it to 'consolidate learnings' helps it manage its context:

"Let's stop and reflect on what we learned.  Update your memory and make sure to udpate mem-fusion as well"

---

## How it works - Mem-Fusion

```
   ┌──────────────────────────────────────────────────────┐
   │                  CLAUDE / LLM SESSION                │
   │            (working memory: in-context tokens)       │
   └─┬─────────────────────────────────────────────┬──────┘
     ▲                                             │
     │ INJECT relevant memories                    │ INGEST decisions,
     │ (semantic match score > 0.75)               │ preferences, errors,
     │                                             │ context, code
     ▼                                             ▼
   ┌─────────────────────────────────────────────────────┐
   │             MEM-FUSION MCP SERVER (9 tools)         │
   │   ┌──────────────┐         ┌──────────────────┐     │
   │   │  VECTOR DB   │  fused  │  MD MEMORY FILES │     │
   │   │  (Qdrant)    │ ──────▶ │  (operating      │     │
   │   │              │         │   principles)    │     │
   │   └──────────────┘         └──────────────────┘     │
   └─────────────────────────────────────────────────────┘
                              ▲
                              │ AUTOMATIC HOOKS
              SessionStart · UserPromptSubmit · Stop · PostToolUse:Write
```

Three storage layers fused into one substrate: in-context working memory ↔ MCP server ↔ Qdrant (vector / episodic / semantic) + markdown files (procedural / operating-principle). The hooks make memory invisible by default — you don't have to explicitly recall or store; the system does it. Manual `/remember` for high-importance items remains available.

---

## How it works - Constellation

```
   ┌──────────────────────────────────────────────────────┐
   │              PEER A (your laptop)                    │
   │   Mem-Fusion + Constellation + local Qdrant          │
   │   ─────────────────────────────────────────────      │
   │   store_memory(..., groups=[engineering])            │
   │     → local entry, origin_node=A, submitted_at=now   │
   └────────────────────────┬─────────────────────────────┘
                            │
                  POST /memory/put { groups: [...] }
                  (push-time filter strips groups
                   recipient isn't a member of)
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
   ┌─────────────────────┐    ┌──────────────────────┐
   │  PEER B (desktop)   │◄──►│  PEER C (teammate)   │
   │                     │    │                      │
   │  same stack         │    │  same stack          │
   │  global dedup       │    │  global dedup        │
   │  on content_hash;   │    │  on content_hash;    │
   │  additive merge of  │    │  additive merge of   │
   │  incoming groups    │    │  incoming groups     │
   └─────────────────────┘    └──────────────────────┘
```

Every peer runs the same stack: Mem-Fusion + Constellation daemon + local Qdrant. When you store a memory on Peer A tagged for a shared group, Constellation sends the full record (content + 768-dim vector + `content_hash` + filtered `groups` list) over HTTP to every peer in that group. Receivers dedup globally on `content_hash` and additively merge incoming groups into existing entries — same content from a different group widens the `groups` list rather than creating a duplicate.

Because group-shared entries live in the same Qdrant collection as your local memories, they surface through the same Mem-Fusion MCP tools — your Claude sees a teammate's stored decision as just another memory, with the original `origin_node` preserved end-to-end (a memory bob pushed and alice relayed to carol still says `origin_node=bob`). There's no orchestrator, no privileged peer, no hub: every peer is symmetric. Any responsive peer in a group can serve the full group history on `/memory/since` pull — so downtime of any single peer doesn't lose data. Trust is by group membership, agreed out-of-band.

---

## Configuring Constellation — adding peers, changing groups

Constellation's config lives at:

```
~/.local/share/mem-fusion/constellation/config.json
```

Open it in any editor; it looks like this:

```json
{
  "node_name": "alice-mac",
  "peer_listen_address":    "0.0.0.0:7533",
  "gateway_listen_address": "127.0.0.1:7534",
  "qdrant_url": "http://127.0.0.1:6333",
  "state_dir":  "~/.local/share/mem-fusion/constellation",
  "memberships": [
    {
      "group_name": "personal",
      "peers": []
    },
    {
      "group_name": "engineering@branch",
      "peers": [
        { "node_name": "bob-mac",      "endpoint": "http://10.0.0.5:7533" },
        { "node_name": "carol-laptop", "endpoint": "http://10.0.0.7:7533" }
      ]
    }
  ]
}
```

Every install gets a `personal` membership pre-populated with empty peers — it's the implicit default for any memory not explicitly tagged for a shared group. To sync your own machines (laptop ↔ desktop), populate `personal.peers` with your other devices' endpoints.

### Add a teammate-group peer

1. Append to the relevant group's `peers[]` with the new peer's `node_name` and HTTP endpoint (their machine's IP/hostname on port 7533).
2. Restart the daemon:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.constellation.plist
   launchctl load   ~/Library/LaunchAgents/com.branchapp.memfusion.constellation.plist
   ```
3. Confirm: `curl http://127.0.0.1:7533/health` should still return `ok: true` and list every configured group in `memberships`.
4. Ask Claude to `/remember pull` — your local store catches up on anything the new peer has already shared.

**Symmetric mesh:** every peer in a group needs every other peer in their `peers[]` list. There's no automatic discovery. If you add a 4th peer (`dave`), then alice, bob, and carol each add `dave` to engineering's peer list, and dave's config lists all three.

### Add a new group

Append a new `{group_name, peers}` entry to `memberships[]` and restart. v0.4 supports any number of memberships per peer — `personal`, one or more team groups (`engineering@branch`, `product@branch`), project-specific groups (`design-system@frontend`), whatever the organization needs.

### Remove a peer

Delete the entry from `peers[]` and restart. Memories previously shared with that peer remain in your local Qdrant — removal only stops future pushes/pulls from them. Note: there is no "un-share" — peers that already received content keep it.

### Check that it's working

After any config change:
```bash
curl -s http://127.0.0.1:7533/peers/self        | python3 -m json.tool
curl -s -X POST http://127.0.0.1:7534/pull -d '{}' -H 'Content-Type: application/json' | python3 -m json.tool
```
The first shows your identity + every configured group membership. The second pulls from every group's peers and reports per-(peer, group) status — handy for verifying a peer endpoint actually responds. Pass `{"group": "engineering@branch"}` to scope to one group.

---

## Customizing Claude for recurring tasks

If you run Claude Code in a scheduled context — cron jobs, daily reviews, weekly summaries, periodic health checks — append this block to your `~/CLAUDE.md` (or a project-level `CLAUDE.md`). It tells Claude to load context from prior runs at the start of each scheduled task and to record outcomes at the end, so each run gets sharper than the last.

````markdown
## Recurring / scheduled tasks

When you're invoked to perform a recurring task (cron, daily review, weekly
report, periodic check, etc.) — i.e., something the user has set up to run
on a schedule rather than asking you ad-hoc:

### At task start
1. Run `search_memory(query="<task name or description>", top_k=8,
   type="session")` to load context from prior runs of this task.
2. Run `search_recent(hours=168)` for weekly tasks, `hours=24` for daily —
   surfaces what changed since the last run.
3. Briefly note in your reasoning what prior runs covered, so you don't
   redo work that was already done.

### At task end
1. Store a session-type memory summarizing what this run did:
   `store_memory(content="<task name>: <one-paragraph summary of what was
   done, what changed since last run, what's still pending>",
   type="session", importance=3, tags=["scheduled", "<task name>"])`
2. If this run produced a new decision, a resolved error, or a stable new
   pattern, store it as its own memory with the appropriate type
   (`decision`, `error`, `code`) and importance ≥ 3.
3. If Constellation is installed and the user wants this learning shared
   with their group, first call `add_groups(memory_ids=[id],
   groups=["<group>"])` to widen the memory's group set, then
   `group_push(group="<group>", memory_ids=[id])`.

Result: recurring tasks compound. Each run starts with what every previous
run learned, builds on it, and leaves the next run a clearer starting point.
````

You can scope this per-task by referencing the task name in `search_memory` queries and `tags=["scheduled", "<task name>"]` on stores. A daily "review yesterday's PRs" task will pick up only the prior review runs; a weekly "release-readiness check" only its prior runs.

---

For full architectural detail:

- [`docs/MEM_FUSION_ARCHITECTURE.md`](docs/MEM_FUSION_ARCHITECTURE.md) — single-node design (storage model, MCP tool surface, hooks, lifecycle)
- [`docs/CONSTELLATION_ARCHITECTURE.md`](docs/CONSTELLATION_ARCHITECTURE.md) — group sharing design (peer protocol, source tagging)
- [`CHANGELOG.md`](CHANGELOG.md) — what's in each version

---

## MCP tools

**10 local-memory tools**: `store_memory` · `search_memory` · `search_recent` · `upsert_memory` · `find_or_create` · `delete_memory` · `get_related` · `memory_stats` · `export_record` · `add_groups`

**2 group tools** (require Constellation): `group_pull` · `group_push`

`store_memory` accepts an optional `groups: list[str]` arg (default `["personal"]`) — the routing key for sharing. `add_groups(memory_ids, groups)` retroactively widens an existing memory's group set (additive only; un-sharing isn't supported because peers already received the content). `group_push(group, memory_ids?)` is always scoped to one group at a time — pushing to multiple groups in a single user turn is multiple calls, never an implicit fan-out. `export_record` returns a memory's full Qdrant record including its 768-dim vector — used by Constellation to propagate memory across machines without re-embedding.

## Memory types

| Type | Captures |
|---|---|
| `decision` | Architecture / tool / approach choices |
| `preference` | User-stated rules ("always X", "never Y") |
| `fact` | Verifiable observations |
| `error` | Resolved errors and their fixes |
| `code` | Significant code patterns / file writes |
| `context` | Background context about an initiative |
| `session` | Auto-extracted session summaries |

Importance: 1 (trivial) → 5 (user-curated, highest priority).

---


## Uninstall

```bash
claude mcp remove mem-fusion
claude mcp remove constellation 2>/dev/null   # if installed
launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist
launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist
launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.constellation.plist 2>/dev/null
rm ~/Library/LaunchAgents/com.branchapp.memfusion.*.plist
rm -rf ~/.local/share/mem-fusion          # ⚠ deletes all stored memories
rm -rf ~/.claude/skills/remember
# Then remove the 4 mem-fusion hook entries from ~/.claude/settings.json
```

Full uninstall sections are at the bottom of each INSTALL file.

---

## License

MIT. See [`LICENSE`](LICENSE).

*Built at [Branch](https://branchapp.com).*
