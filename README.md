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
2. Open [`INSTALL_MEM_FUSION.md`](INSTALL_MEM_FUSION.md), copy the entire file, paste it as your first message.
3. Approve the commands through the 15 steps.

Installs Qdrant 1.13.4, Ollama + `nomic-embed-text`, the Mem-Fusion MCP server, 4 Claude Code hooks, and the `/remember` skill. ~200 MB on disk.

### 2. Group memory across machines (optional, ~5 min per peer)

Install Mem-Fusion first on each machine. Then on each one:

1. Open [`INSTALL_CONSTELLATION.md`](INSTALL_CONSTELLATION.md), copy, paste into Claude Code.
2. Approve the steps. Every peer joins as a symmetric mesh participant — nothing to designate, no central node, no special configuration per machine.

Constellation runs as a persistent HTTP MCP daemon (port 7533) alongside Mem-Fusion. Federation entries land in the same Qdrant collection as your local memories, tagged `source=federation`.

> v0.3.0 ships **auto-promote** by default — every locally-stored memory propagates to the rest of the mesh. A human-review / apprenticeship loop is deferred to post-MVP.

---

## How it works

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

With Constellation enabled, a second daemon federates memory across peers via HTTP, using the same Qdrant collection with a `source` payload tag distinguishing local from federated entries.

For full architectural detail:

- [`docs/MEM_FUSION_ARCHITECTURE.md`](docs/MEM_FUSION_ARCHITECTURE.md) — single-node design (storage model, MCP tool surface, hooks, lifecycle)
- [`docs/CONSTELLATION_ARCHITECTURE.md`](docs/CONSTELLATION_ARCHITECTURE.md) — federation design (mesh topology, peer protocol, source tagging)
- [`CHANGELOG.md`](CHANGELOG.md) — what's in each version

---

## MCP tools (9)

`store_memory` · `search_memory` · `search_recent` · `upsert_memory` · `find_or_create` · `delete_memory` · `get_related` · `memory_stats` · `export_record`

`export_record` returns a memory's full Qdrant record including its 768-dim vector — used by Constellation to propagate memory across machines without re-embedding.

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

## Configure Claude to use it

Paste this into `~/CLAUDE.md` (or a project-level `CLAUDE.md`) so Claude calls the tools at the right moments:

````markdown
## Vector Memory System

Connected to a local `mem-fusion` MCP server (Qdrant + nomic-embed-text on localhost).
Check at session start: `memory_stats()`.

### When to search
- Session start: `search_memory(query="<current task>", top_k=8)`
- Before architectural decisions: search for prior decisions on the same topic
- On recurring errors: search prior resolutions
- Unsure about a user preference: search type="preference"

### When to store
- Decision made: `store_memory(content, type="decision", importance=4, project="<name>")`
- Novel error resolved: `store_memory(..., type="error", importance=3)`
- User reveals a preference: `store_memory(..., type="preference", importance=4)`
- Important context learned: `store_memory(..., type="context", importance=3)`
- User explicitly asks to remember: use `/remember` → importance=5

### What NOT to store
Trivial facts, transient state, things derivable from code or `git log`.

### Verification rule
Memories are point-in-time observations. Before recommending a file/function/flag named in a memory, verify it still exists in the current code.
````

INSTALL_CONSTELLATION.md adds further CLAUDE.md content for federation — the install prompt drops it in automatically.

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
