# Changelog

All notable changes to Mem-Fusion will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased] — planned for v0.3.0

Design locked; implementation in progress. v0.3.0 is the next release after v0.1.0 — purely additive (no breaking changes to existing tool names, hooks, or storage format). v0.1.0 users will upgrade by re-pasting the updated `INSTALL.md` prompt into Claude Code; the upgrade detects the existing v0.1.0 install and patches it in place. The Constellation daemon installs as an opt-in companion.

### Added

- **Group memory via bundled Constellation daemon** — a separate, persistent HTTP MCP daemon shipped in this repo at `extensions/constellation/`. Enables federated group memory across multiple Mem-Fusion nodes. Each group has one orchestrating node + N member nodes. Memory submitted to a group's orchestrator is auto-accepted into that group's canonical store. Sovereign per-group Qdrant collections (not replicated databases; selective propagation between independent stores).
- **`export_record(id)` tool in Mem-Fusion** — returns a stored memory's full Qdrant record including its 768-dim vector. Enables faithful promotion of a local memory into a group canonical without re-embedding (vector copied verbatim).
- **Constellation MCP tool surface** (4 tools, all under the `constellation/` namespace): `memory/put`, `memory/get`, `peers`, `peers/self`. Content_hash integrity verified at destination; per-group dedup; provenance chain preserved.
- **Two-daemon architecture** — Mem-Fusion stays stdio (per-Claude-session subprocess, localhost-only). Constellation runs as a persistent HTTP MCP daemon under launchd (`com.branchapp.memfusion.constellation`). Disjoint Qdrant collections; Constellation has no privileged access to Mem-Fusion's local memory.
- **Claude bridge pattern (CLAUDE.md instruction)** — Claude is instructed to perform a three-call sequence on user "remember X" requests: `store_memory` (local) → `export_record` (extract full record) → `constellation/memory/put` (promote to group canonical). Auto-promotion is the v0.3.0 default; private retention is deferred to post-MVP.

### Changed

- **Qdrant port 6333 pre-flight: hard bail** — port-in-use is now an installer error, not a warning. Mem-Fusion requires exclusive use of Qdrant for its stateful collection. Constellation uses the same Qdrant instance with a separate collection.
- **Ollama port 11434 pre-flight: smart reuse** — if port 11434 is taken by an existing Ollama instance, the installer verifies it's actually Ollama, ensures `nomic-embed-text` is loaded (pulls if missing), confirms a 768-dim embedding round-trip, and consumes that Ollama without installing a duplicate launchd plist. Hard bail if the port is taken by something other than Ollama.

### Fixed

- **`init_collection.py` ignored `QDRANT_URL` env var** — v0.1.0 had `QDRANT_URL = "http://127.0.0.1:6333"` hardcoded, so setting the env var had no effect. Now respects `os.getenv("QDRANT_URL", "http://127.0.0.1:6333")` symmetrically with `MEMFUSION_COLLECTION`. Latent in v0.1.0; surfaced when running init against a non-default Qdrant port (e.g., a dev / test peer).

### Architecture notes

- **Object model**: two entities — node and group. "Orchestrating" is a per-group role, not a node type. A node can be a non-orchestrating member of one group and the orchestrating node of another. v0.3.0 ships with single-group-per-node configurations; data structures preserve multi-group futures.
- **Routing rules** (load-bearing invariants): direct send only (no relays), membership equals authorization, no transitive routing, no automatic cross-group propagation.
- **Replication model**: each group's canonical Qdrant collection is sovereign. Different orchestrators have different canonical contents — not replicas of each other. Selective propagation through deliberate `memory/put` calls; no expectation of eventual convergence across the network.
- **Forward compatibility**: schemas accommodate post-MVP features (multi-group membership, hierarchy via orchestrator-as-member-of-parent-group, cross-group propagation) without code support; post-MVP implementation can extend without breaking v0.3.0 wire formats.

### Considered and deferred (post-MVP)

- **Human review queue / curator / apprenticeship loop** — v0.3.0 auto-accepts all valid memory submissions. Per-orchestrator human review with classifier-mediated escalation is post-MVP.
- **Multi-group membership per node** — v0.3.0 supports one group per node. Multi-membership and the hierarchical orchestrator pattern land in post-MVP.
- **Cross-group memory propagation** — the architecture supports orchestrator-as-member-of-parent-group, but v0.3.0 doesn't implement the cross-group routing logic.
- **Per-peer cryptographic identity** — v0.3.0 uses a shared swarm key per group. mTLS / per-node Ed25519 keypair authentication is post-MVP.
- **Native OS notifications** (macOS Notification Center, etc.) — v0.3.0 uses Claude-native channels only (SessionStart hooks, MCP tools). System notifications can come later when the curator review flow lands.
- **Private memory flag at promotion time** — v0.3.0 auto-promotes all user-initiated `store_memory` calls. Per-memory "stay local" flags are post-MVP.
- **In-process extension loader for Mem-Fusion** — was considered for the "Constellation as in-process extension" model. Resolved: Constellation runs as a separate daemon, so the in-process loader isn't needed for v0.3.0. May land in a later version if other in-process extensions emerge.
- **FastMCP migration** — the official MCP SDK is sufficient; FastMCP composition primitives are interesting but not yet load-bearing.

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
- Single-machine deployment (multi-node coordination is bundled in v0.3.0 via Constellation)
- Hooks tied to Claude Code's specific hook event names (SessionStart, UserPromptSubmit, Stop, PostToolUse)

### Known issues (to be fixed in v0.3.0)

- `init_collection.py` ignores the `QDRANT_URL` env var; silently talks to `http://127.0.0.1:6333` regardless. Only surfaces when running init against a non-default port. See [Unreleased] § Fixed.

---

[Unreleased]: https://github.com/muycoreano/mem-fusion/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/muycoreano/mem-fusion/releases/tag/v0.1.0
