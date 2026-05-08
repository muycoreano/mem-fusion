# Mem-Fusion

**Local persistent memory for Claude Code. Vector recall + durable rules + working context, fused into one layer that learns across sessions.**

Out of the box, Claude Code has only a context window — every session starts blank, decisions evaporate, preferences must be re-stated, and resolved errors recur. Mem-Fusion fixes that by giving Claude long-term, semantically-searchable memory that survives across sessions, runs entirely on your Mac, and learns silently in the background.

After install:

- **Decisions, preferences, errors, and context are stored automatically** — you don't manage memory; it manages itself.
- **Every prompt is matched against your memory store** and relevant hits are silently injected into Claude's context.
- **`/remember <thing>`** pins something important with a single command.
- **Cross-session continuity** — a decision you made last Tuesday surfaces in today's session, automatically.
- **No data leaves your Mac.** Qdrant + Ollama + a Python MCP server, all localhost.

---

## Install

The install is AI-native: there's no shell installer to run yourself. Instead, you paste a setup prompt into Claude Code and Claude does the install for you, asking your approval at each step.

1. Open Claude Code in a terminal on your Mac.
2. Open [`INSTALL.md`](INSTALL.md) in this repo.
3. Copy its entire contents.
4. Paste it as your first message in Claude Code.
5. Approve the commands as Claude works through the 14 steps.

That's it. Claude will install Qdrant, Ollama, the embedding model, the MCP server, the four hooks, and the `/remember` skill — and run a smoke test before reporting success.

> Requires macOS, Homebrew, and Claude Code already installed. ~10 minutes end to end. ~200 MB on disk.

---

## What you get

| Component | What it does |
|---|---|
| **Qdrant 1.13.4** | Local vector database (port 6333) |
| **Ollama 0.20.5 + `nomic-embed-text`** | Local 768-dim embeddings (port 11434) |
| **Mem-Fusion MCP server** | Exposes 8 memory tools to Claude Code via stdio |
| **4 Claude Code hooks** | SessionStart, UserPromptSubmit, Stop, PostToolUse:Write — all automatic |
| **`/remember` skill** | One-command pinning for high-importance memory |

The eight MCP tools:

`store_memory` · `search_memory` · `search_recent` · `upsert_memory` · `find_or_create` · `delete_memory` · `get_related` · `memory_stats`

---

## Memory types

Mem-Fusion classifies memories into seven types, each with its own retention and retrieval semantics:

| Type | What it captures |
|---|---|
| `decision` | Choices made (architecture, tool selection, approach) |
| `preference` | User-stated rules ("always X", "never Y") |
| `fact` | Verifiable observations |
| `error` | Resolved errors and their fixes |
| `code` | Significant code patterns / file writes |
| `context` | Background context about an initiative |
| `session` | Auto-extracted session summaries |

Importance is 1 (trivial) → 5 (user-curated, highest priority).

---

## How it works (in 30 seconds)

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
   │             MEM-FUSION MCP SERVER                   │
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

Three layers, fused: in-context working memory ↔ MCP server (8 tools) ↔ Qdrant (vector / episodic / semantic) + markdown files (procedural / operating-principle).

The hooks make memory **invisible by default** — you don't have to explicitly recall or store; the system does it. Manual `/remember` for high-importance items remains available.

---

## Configure Claude to use it

After running [`INSTALL.md`](INSTALL.md), paste this block into your `~/CLAUDE.md` (or a project-level `CLAUDE.md`) so Claude knows when to call the memory tools:

````markdown
## Vector Memory System

Connected to a local `mem-fusion` MCP server (Qdrant + nomic-embed-text on localhost).
**Check this at session start** by calling `memory_stats()` to confirm the system is live.

### When to search
- **Session start**: `search_memory(query="<current task>", top_k=8)`
- **Before architectural decisions**: search for prior decisions on the same topic
- **When hitting a recurring error**: search for prior resolutions
- **When unsure about a user preference**: search type="preference"

### When to store
- **Decision made**: `store_memory(content, type="decision", importance=4, project="<name>")`
- **Novel error resolved**: `store_memory(..., type="error", importance=3)`
- **User reveals a preference**: `store_memory(..., type="preference", importance=4)`
- **Important context learned**: `store_memory(..., type="context", importance=3)`
- **User explicitly asks to remember**: use `/remember` skill → `importance=5`

### What NOT to store
Trivial facts, transient state, things derivable from code or `git log`.

### Verification rule
Memories are point-in-time observations. Before recommending a file/function/flag named in
a memory, verify it still exists in the current code.
````

---

## What's intentionally not included

- **No telemetry, no phone-home, no cloud.** Everything is localhost-only.
- **No Linux/Windows support.** macOS launchd is required by the current architecture.
- **No multi-machine sync.** That's a sibling project ([Constellation](#related)) — a federated multi-node architecture that uses Mem-Fusion as the per-node memory layer.

---

## Uninstall

```bash
claude mcp remove mem-fusion
launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist
launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist
rm ~/Library/LaunchAgents/com.branchapp.memfusion.*.plist
rm -rf ~/.local/share/mem-fusion         # ⚠ deletes all stored memories
rm -rf ~/.claude/skills/remember
# Then remove the 4 mem-fusion entries from ~/.claude/settings.json (or restore from .bak)
```

Full uninstall instructions are at the bottom of [`INSTALL.md`](INSTALL.md).

---

## Related

- **Constellation** — federated multi-node coordination architecture for distributed AI agents. Each node runs a full Mem-Fusion stack; Constellation handles cross-node coordination. *(Coming soon.)*

---

## License

MIT. See [`LICENSE`](LICENSE).

---

*Built at [Branch](https://branchapp.com).*
