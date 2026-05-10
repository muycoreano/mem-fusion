# Changelog

All notable changes to Mem-Fusion will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased] — planned for v0.2.0

Design locked; implementation not yet started. v0.2.0 is purely additive — **no breaking changes** to v0.1.0 tool names, hooks, or storage format. v0.1.0 users will upgrade by re-pasting the updated `INSTALL.md` prompt into Claude Code; the upgrade detects the existing v0.1.0 install and patches it in place.

### Added

- **Extension loader** — `mcp_server.py` scans `~/.local/share/mem-fusion/extensions/` at startup and loads any Python module that follows the extension convention. Each extension declares `NAMESPACE = "<prefix>"`, a `TOOLS` list of MCP `Tool` objects, and an `async def dispatch(name, args)` handler. Enables third-party tooling (e.g., Constellation) to add tools without forking Mem-Fusion. ~30 LOC of loader code, no framework dependency.

- **Namespace prefix discipline** — every extension tool must be named `<namespace>/<tool>`. The loader rejects extensions that:
  - declare tools without the namespace prefix,
  - declare a namespace already claimed by another extension,
  - declare tools that collide with Mem-Fusion's top-level tools.
  Top-level naming is grandfathered for Mem-Fusion's own 8 tools — see *Naming model* below.

- **Install manifest** — `~/.local/share/mem-fusion/install_manifest.json` records which services Mem-Fusion installed (owned) vs. which it consumed from the user's environment (shared, e.g., a pre-existing Ollama). Used by upgrade and uninstall logic to manage only what we own.

### Changed

- **Qdrant port 6333 pre-flight: hard bail** — port-in-use is now an installer error, not a warning. Mem-Fusion requires exclusive use of Qdrant for its stateful collection; sharing isn't safe. Clear error tells the user to stop the conflicting service. (Multi-Qdrant support is not on the roadmap; per-collection isolation handles the use cases we've encountered.)

- **Ollama port 11434 pre-flight: smart reuse** — if port 11434 is taken by an existing Ollama instance, the installer:
  1. Verifies it's actually Ollama (queries `/api/tags`),
  2. Checks `nomic-embed-text` is loaded; pulls it if missing,
  3. Verifies a 768-dim embedding round-trip,
  4. Consumes that Ollama without installing a duplicate launchd plist.

  If port 11434 is taken by something other than Ollama, hard bail. Sharing is safe because Ollama is stateless and the model is identical across consumers.

### Fixed

- **`init_collection.py` ignored `QDRANT_URL` env var** — v0.1.0 had `QDRANT_URL = "http://127.0.0.1:6333"` hardcoded, so setting the env var had no effect. The script silently talked to whatever Qdrant was on the default port and reported success. Now respects `os.getenv("QDRANT_URL", "http://127.0.0.1:6333")` symmetrically with `MEMFUSION_COLLECTION`. Latent in v0.1.0; only surfaced when running init against a non-default port (e.g., a dev / test peer). Discovered while building Constellation's dev tooling.

### Naming model (locked in v0.2.0)

| Tools | Convention | Examples |
|---|---|---|
| Mem-Fusion (the host) | Flat, no prefix — grandfathered | `store_memory`, `search_memory`, `memory_stats`, etc. |
| Any extension | Under its own namespace prefix | `constellation/submit_candidate`, `linear-bridge/sync_issue` |

The grandfather clause means v0.1.0 users' CLAUDE.md snippets continue to work. A future major version may re-prefix Mem-Fusion's own tools under `memory/`, but that's not on the v0.2 roadmap.

### Considered and deferred

- **Migrate to FastMCP** — its `import_server(prefix=...)` composition would replace the hand-rolled loader. Deferred: FastMCP is converging with the official MCP SDK but isn't yet a de facto standard; the ~30-LOC DIY loader is small, well-understood, and easily replaced later. We'll re-evaluate when extension count grows past ~3 or when FastMCP adoption broadens.

- **Mem-Fusion tool rename to `memory/<tool>`** — symmetrical with extensions, but breaking for v0.1.0 users. Not worth the migration cost at our adoption level until there's a strong reason.

- **Multi-node coordination** — explicitly out of scope for Mem-Fusion. Distributed agent coordination lives in a sibling project (Constellation), which uses Mem-Fusion as the per-node memory layer via the v0.2.0 extension API. Mem-Fusion stays single-machine; Constellation handles federation.

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

### Known issues (to be fixed in v0.2.0)

- `init_collection.py` ignores the `QDRANT_URL` env var; silently talks to `http://127.0.0.1:6333` regardless. Only surfaces when running init against a non-default port. See [Unreleased] § Fixed.

---

[Unreleased]: https://github.com/muycoreano/mem-fusion/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/muycoreano/mem-fusion/releases/tag/v0.1.0
