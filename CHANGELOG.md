# Changelog

All notable changes to Mem-Fusion will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] — 2026-05-08

Initial public release.

### Added

- **AI-native install** — `INSTALL.md` is a setup prompt users paste into Claude Code; Claude reads it and runs the install for the user, asking approval at each step. No bash installer to debug.
- **8 MCP tools**: `store_memory`, `search_memory`, `search_recent`, `upsert_memory`, `find_or_create`, `delete_memory`, `get_related`, `memory_stats`
- **4 hooks** wired into Claude Code's hook system:
  - `SessionStart` — primes context with recent memory snapshot (last 48h)
  - `UserPromptSubmit` — semantic search on every prompt; injects relevant memories scoring >0.75
  - `Stop` — extracts decisions, preferences, errors from the session transcript and stores them in the background
  - `PostToolUse(Write)` — captures new files >100 lines as `code` memories
- **Bundled `/remember` skill** for high-importance, user-curated memory storage (`importance=5`)
- **7 memory types** with distinct retention semantics: `decision`, `fact`, `preference`, `error`, `code`, `context`, `session`
- **Importance scale** 1 (trivial) → 5 (user-curated, highest priority)
- **Project tagging** for multi-tenant memory across initiatives
- **Content-hash deduplication** — exact-content collisions skipped automatically
- **Local-only operation** — Qdrant 1.13.4 + Ollama 0.20.5 + `nomic-embed-text` (768-dim) all on localhost. No telemetry, no cloud, no SaaS lock-in.
- **macOS launchd integration** — Qdrant and Ollama run as user-level services (`com.branchapp.memfusion.qdrant`, `com.branchapp.memfusion.ollama`) with `KeepAlive` and proper resource limits
- **Configurable collection name** via `MEMFUSION_COLLECTION` env var (defaults to `mem_fusion_memories`)
- **CLAUDE.md snippet** in the README for teaching Claude when to call the memory tools
- **MIT license**

### Architecture

Three layers fused via automatic hooks:

| Layer | Storage | Cognitive analogue |
|---|---|---|
| Working memory | LLM session context | Working memory |
| Vector | Qdrant (semantic search, 768-dim cosine) | Episodic + semantic |
| Markdown | `~/.claude/.../memory/MEMORY.md` | Procedural + operating principles |

### Known limitations

- macOS only (launchd hard-required by current architecture)
- Single-machine deployment (multi-node coordination is a separate sibling project)
- Hooks tied to Claude Code's specific hook event names (SessionStart, UserPromptSubmit, Stop, PostToolUse)

---

[0.1.0]: https://github.com/muycoreano/mem-fusion/releases/tag/v0.1.0
