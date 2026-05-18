# Mem-Fusion

**Persistent memory for Claude Code — across sessions, across the productivity tools you already use.**

Out of the box, Claude Code is brilliant in the moment and amnesic between moments. Every session starts blank. Yesterday's architectural decision, last week's stated preference, last month's hard-won bug fix — all gone. You re-explain your codebase. You re-establish your conventions. You re-debug the same errors. The smarter Claude gets as a model, the more painful the reset becomes, because what you're losing each time is more valuable.

**Mem-Fusion fixes this.** A local memory layer watches your Claude Code sessions, captures decisions and patterns as you work, and surfaces them automatically the next time they're relevant. Most of the time it's invisible — Claude finds and uses what it needs, when it needs it — but `/remember` is there when you want to pin something explicitly. The longer you use it, the sharper your Claude gets at *your* work, in *your* codebase, with *your* preferences. Everything runs on your machine; nothing leaves it.

When you want memories to flow between your own machines or across a team, Mem-Fusion has two paths:

- **Connectors (v0.5, in design)** — bridges to cloud productivity tools you already use (Slack first; GDrive, Teams, Discord, and more coming). The productivity tool's substrate handles auth and access control; mem-fusion handles the wire format and dedup. See the [Connectors](#connectors-preview-v05) section below.
- **Constellation (today, optional)** — a peer-to-peer sibling system for direct machine-to-machine memory sync. Useful when cloud tools aren't an option (corporate environments, fully-private mesh, offline). See [`constellation/README.md`](constellation/README.md).

Both implement the same mem-fusion sharing API (push/pull/status/since). You can run neither, one, or both.

---

## Install

One curl line. Requires macOS + Homebrew + Claude Code already installed.

```bash
curl -fsSL https://raw.githubusercontent.com/muycoreano/mem-fusion/v0.5/install.sh | bash
```

Or paste into any Claude Code session:

> *"Install mem-fusion by running: `curl -fsSL https://raw.githubusercontent.com/muycoreano/mem-fusion/v0.5/install.sh | bash`"*

The script is idempotent — re-run it any time to upgrade. It clones to `~/dev/mem-fusion` (overridable via `MEMFUSION_CLONE_DIR`) and brings up Qdrant 1.13.4, Ollama + `nomic-embed-text`, the Mem-Fusion MCP server, 4 Claude Code hooks, and 2 skills (`remember`, `mem-fusion-slack-connector`). ~200 MB on disk.

After install, restart Claude Code and ask about setting up the **mem-fusion Slack connector** if you want cross-machine memory sharing through a Slack channel. For peer-to-peer LAN sync as an alternative, see [`constellation/README.md`](constellation/README.md).

### What gets installed

- `~/.local/share/mem-fusion/` — Qdrant data, Python venv, the MCP server, helper scripts, fix-script artifacts.
- `~/Library/LaunchAgents/com.branchapp.memfusion.{qdrant,ollama,constellation}.plist` — daemons.
- `~/.claude/skills/{remember,mem-fusion-slack-connector}/` — slash-command + connector-operations skills.
- `~/.claude/settings.json` (anchor-patched between `<!-- mem-fusion:* -->` markers) — 4 hooks.
- `~/CLAUDE.md` (anchor-patched) — `## Vector Memory System` section telling Claude how to use the store.

### Upgrade

Re-run the same install command. install.sh is idempotent — fresh install, partial upgrade, and fully-current install all execute the same command and produce the right result.

### Uninstall

```bash
launchctl bootout gui/$UID/com.branchapp.memfusion.{qdrant,ollama,constellation}
rm -rf ~/Library/LaunchAgents/com.branchapp.memfusion.*.plist ~/.local/share/mem-fusion
claude mcp remove mem-fusion
```

Then strip the `<!-- mem-fusion:* -->` blocks from `~/CLAUDE.md` and `~/.claude/settings.json`.

---

## Using it day-to-day

Most memory operations are invisible — the four Claude Code hooks (`SessionStart`, `UserPromptSubmit`, `Stop`, `PostToolUse:Write`) inject relevant context before Claude responds and capture decisions, errors, preferences, and code patterns as you work. You don't have to remember to recall or store; the system does it.

`/remember` is the explicit pin — use it when you've just made a decision (or stated a preference) you want Claude to act on going forward.

### `/remember` examples (local-only, no sharing configured)

```
You:   /remember always use uv for Python environments

Claude: ✓ Stored as preference (id: a7e3c2d1).
        Local-only — nothing leaves this machine.
```

```
You:   "Let's pause and store what we learned this morning."

Claude: ✓ Stored 4 memories locally.
            - decision: gRPC for internal RPC (id: a7e3)
            - decision: Postgres 16 with logical replication (id: b8f4)
            - error:    JWT clock-skew fix (id: c9a5)
            - context:  auth migration timeline (id: d0b6)
```

### How memories surface

You don't need to explicitly recall a memory. The `UserPromptSubmit` hook embeds your message and searches Qdrant for relevant memories above a similarity threshold (0.75 by default) — anything that matches is injected into Claude's context before it responds. Manual recall is always available — just ask: *"What did we decide about the auth service?"* triggers Claude to call `search_memory(...)` and surface the relevant entry.

### Prompt tip — periodically consolidate your learnings

After working 2–3 hours straight with Claude, it tends to get a bit 'loose' naturally with and without mem-fusion. Telling it to 'consolidate learnings' helps it manage its context:

> *"Let's stop and reflect on what we learned. Update your memory and make sure to update mem-fusion as well."*

---

## Connectors (preview, v0.5)

The connector model is mem-fusion's path to sharing memories through productivity tools you already use. Slack ships as the bundled default example in v0.5; additional connectors (GDrive, Teams, Discord, Notion, custom) plug into the same sharing API.

### The idea

A **connector** is a declared bridge between local mem-fusion and one productivity-tool destination. Each connector targets a single substrate (a Slack channel, a GDrive folder, etc.) and implements the four-method sharing API: `push`, `pull`, `status`, `since`.

Memories are local-by-default. To share, you tag a memory with one or more connector IDs and push it explicitly.

### Config

Connectors live in `~/.local/share/mem-fusion/connector.json`:

```json
{
  "connectors": [
    {"id": "engineering",      "type": "slack", "channel": "mem_fusion_engineering"},
    {"id": "marketing",        "type": "slack", "channel": "mem_fusion_marketing"},
    {"id": "personal-devices", "type": "slack", "channel": "mem_fusion_mitch_devices"}
  ]
}
```

Reserved fields: `id` (user-chosen identifier), `type` (which connector handles it). Each `type` declares its own additional fields — `slack` uses `channel`; future types (`gdrive`, `teams`, `discord`, `notion`) declare their own.

### Usage

```
You:   /remember Postgres 16 with logical replication for auth, for engineering

Claude: ✓ Stored locally (id: a7e3, connector_ids: [engineering]).
        ✓ Posted to engineering (#mem_fusion_engineering).
```

```
You:   /remember pull engineering

Claude: ✓ Pulled from engineering: 3 new memories
            (2 from alice-mac, 1 from bob-laptop).
```

### Status

v0.5 is in active design. Slack is the proof-of-concept connector that ships first. The full design is at [`docs/v0.5_CONNECTOR_ARCHITECTURE.md`](docs/v0.5_CONNECTOR_ARCHITECTURE.md).

Want to write your own connector once the SDK lands? The contract is the four-method sharing API; configuration is whatever your substrate needs. Email mailing lists, S3 buckets, IPFS topics, custom protocols — all plausible. The SDK doc is forthcoming.

---

## Peer-to-peer (Constellation)

For users who can't or don't want to use cloud productivity tools as a sharing substrate, the optional **Constellation** sibling system provides direct peer-to-peer memory sync over LAN HTTP. It implements the same sharing API as cloud connectors — same `push`/`pull`/`status`/`since` semantics — but routes through a configured peer mesh instead of a productivity tool.

Use Constellation when:
- Corporate environments where cloud productivity tools aren't approved for the content type.
- Fully private mesh — your machines only, no third party in the loop.
- Offline / air-gapped or strict-data-residency setups.

See [`constellation/README.md`](constellation/README.md) for the full architecture, install steps, peer configuration, and backward-compatibility notes.

---

## How it works — Mem-Fusion

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
   │             MEM-FUSION MCP SERVER (10 tools)        │
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

When sharing is enabled (via Connectors or Constellation), the same memory surface extends across machines. Pulled memories live in the same Qdrant collection as local memories — Claude sees them as just another memory, with the original `origin_node` preserved.

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

Result: recurring tasks compound. Each run starts with what every previous
run learned, builds on it, and leaves the next run a clearer starting point.
````

You can scope this per-task by referencing the task name in `search_memory` queries and `tags=["scheduled", "<task name>"]` on stores.

---

## MCP tools (local-memory)

10 local-memory tools: `store_memory` · `search_memory` · `search_recent` · `upsert_memory` · `find_or_create` · `delete_memory` · `get_related` · `memory_stats` · `export_record` · `add_groups`

`store_memory` accepts an optional `groups: list[str]` arg (read tolerantly for backward compatibility; the v0.5 connector model uses `connector_ids` going forward). `export_record` returns a memory's full Qdrant record including its 768-dim vector — used by both Connectors and Constellation to propagate memory across machines without re-embedding.

**Constellation adds 2 group tools** when installed: `group_pull` · `group_push`. See [`constellation/README.md`](constellation/README.md).

---

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

## Further reading

- [`docs/MEM_FUSION_ARCHITECTURE.md`](docs/MEM_FUSION_ARCHITECTURE.md) — single-node design (storage model, MCP tool surface, hooks, lifecycle)
- [`docs/v0.5_CONNECTOR_ARCHITECTURE.md`](docs/v0.5_CONNECTOR_ARCHITECTURE.md) — v0.5 connector architecture (Slack first, more coming)
- [`constellation/README.md`](constellation/README.md) — Constellation P2P sibling system
- [`CHANGELOG.md`](CHANGELOG.md) — what's in each version

---

## Uninstall

To remove just mem-fusion (keeping any Constellation install for later cleanup):

```bash
claude mcp remove mem-fusion
launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist
launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist
rm ~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist
rm ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist
rm -rf ~/.local/share/mem-fusion          # ⚠ deletes all stored memories
rm -rf ~/.claude/skills/remember
# Then remove the 4 mem-fusion hook entries from ~/.claude/settings.json
```

For Constellation uninstall steps, see [`constellation/README.md`](constellation/README.md#uninstall).

Full uninstall sections are also at the bottom of each INSTALL file.

---

## License

MIT. See [`LICENSE`](LICENSE).

*Built at [Branch](https://branchapp.com).*
