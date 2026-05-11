# Mem-Fusion

**Memory fusion for Claude Code — across sessions, across cognitive layers, and across machines.**

Out of the box, Claude Code has only a context window. Every session starts blank. Decisions evaporate. Preferences must be re-stated. Resolved errors recur. Worse: when you build a useful AI workflow on one machine, it's stranded there — your other Macs, your team, your future self all start over.

Mem-Fusion fuses memory across three axes:

- **Across sessions** — Tuesday's decision surfaces in Friday's session, automatically.
- **Across cognitive layers** — episodic, semantic, procedural, and working memory fused into one substrate via 9 memory tools and 4 automatic hooks.
- **Across machines** *(when you enable the Constellation extension — shipping in v0.3.0)* — personal memory streams from peer machines fuse into curated group memory, governed by an apprenticeship loop that learns your judgment over time.

Same substrate, three axes of fusion. Standalone fuses two of them; turn on Constellation to add the third.

---

## Why this matters

Every team using AI in 2026 has the same hidden bug: **the AI is learning, but only for one person at a time.**

When your sharpest engineer figures out how to debug a tricky issue, their AI knows. Your AI doesn't. When your most AI-fluent PM develops the right instinct for a recurring tradeoff, no colleague's agent benefits. When that person leaves, their AI capability leaves with them.

AI memory is currently a personal asset — trapped per-individual, per-machine, per-tool. Teams using AI heavily are getting more productive, but the productivity stays individual. Every conversation produces learning; almost none of it propagates. **The gap between a team's best AI user and its median isn't closing — it's widening**, because AI fluency compounds inside one head but doesn't transfer.

Mem-Fusion makes AI memory a *team* asset. Same memory layer runs locally on every team member's machine. Lessons fuse into a shared, human-curated group memory store. Your best engineer's workflow can be invoked by a colleague who has never seen it. Knowledge survives role changes and turnover. Compliance stays intact because every cross-person memory passes through an apprenticeship-loop curator — the human decides what propagates.

The next decade's productivity divide won't be "people who use AI vs. people who don't." It'll be **organizations whose AI memory compounds vs. organizations whose AI memory resets every Monday.** Mem-Fusion is the substrate for the first kind.

---

## Two scopes of one mission

| Scope | Axes active | What it delivers | Status |
|---|---|---|---|
| **Personal memory** *(standalone, default)* | Sessions + cognitive layers | Your AI remembers across sessions on this machine | ✅ Available now (v0.1.0) |
| **Group memory** *(Constellation extension)* | + across machines | Peers' learnings fuse into shared, curated memory | 🚧 Shipping in v0.3.0 |

---

## Install (personal memory, today)

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
| **Mem-Fusion MCP server** | Exposes 9 memory tools to Claude Code via stdio |
| **4 Claude Code hooks** | SessionStart, UserPromptSubmit, Stop, PostToolUse:Write — all automatic |
| **`/remember` skill** | One-command pinning for high-importance memory |

The nine MCP tools:

`store_memory` · `search_memory` · `search_recent` · `upsert_memory` · `find_or_create` · `delete_memory` · `get_related` · `memory_stats` · `export_record`

(`export_record` returns a stored memory's full Qdrant record including its vector — used by the Constellation extension to faithfully propagate memory across machines without re-embedding.)

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

## How fusion works (per node)

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

Three storage layers fused into one substrate: in-context working memory ↔ MCP server (8 tools) ↔ Qdrant (vector / episodic / semantic) + markdown files (procedural / operating-principle).

The hooks make memory **invisible by default** — you don't have to explicitly recall or store; the system does it. Manual `/remember` for high-importance items remains available.

When you enable Constellation (v0.3.0), the same per-node substrate fuses *horizontally* with peer nodes through a curator-mediated propagation layer. The vertical and temporal fusion stays exactly as it works today; the third axis just turns on.

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
- **No group memory yet** — that's the v0.3.0 Constellation bundle. Single-machine memory today; multi-machine memory soon.

---

## Roadmap

| Version | What | Status |
|---|---|---|
| **v0.1.0** | Personal memory (sessions + cognitive layers) | ✅ Shipped 2026-05-08 |
| **v0.3.0** | Bundled Constellation daemon — group memory across peers via federated per-group Qdrant collections + `export_record` tool for faithful memory promotion | 🚧 In progress; see [CHANGELOG](CHANGELOG.md) for design and scope |
| **v0.4.0+** | Human review queue (apprenticeship loop), multi-group membership, hierarchical orchestrators, per-peer cryptographic identity, native OS notifications | 📋 Design sketched; not yet scoped for implementation |

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

## License

MIT. See [`LICENSE`](LICENSE).

---

*Built at [Branch](https://branchapp.com).*
