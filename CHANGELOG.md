# Changelog

All notable changes to Mem-Fusion will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased] — v0.5.0 (current on `v0.5` branch)

v0.5 expands Constellation into a pluggable-driver architecture so peer transports beyond HTTP (Slack, S3, etc.) can be wired in without changing the user-facing push/pull surface, replaces the paste-into-Claude install flow with a proper `install.sh`, and corrects the `/remember` autopush semantics around the `personal` group. Pre-alpha — work in progress on the `v0.5` branch.

### Changed

- **`/remember` autopush now triggers for any group with configured peers, including `personal`.** Previously the skill only auto-pushed when groups other than `personal` were named — which broke cross-machine personal sync (same user, multiple Macs): `personal`-tagged memories never propagated despite peers being configured. The skill now calls `group_push(group=G, memory_ids=[id])` for every group `G` in the memory's `groups` list that has peers in the local Constellation config. (WP-1)
- **Bare `/remember push` (no argument) now pushes the `personal` group only.** Equivalent to `/remember push personal`. This is the **privacy invariant**: non-personal groups never propagate without explicit naming. Previously bare `/remember push` was documented as a bulk push across every configured group — a privacy footgun if a memory happened to be tagged with multiple groups. (WP-1)
- **`/remember` skill description and routing-rule wording** updated consistently in `src/skills/remember/SKILL.md` to reflect the new semantics. Hook-captured memories remain local-only by design — hooks call `store_memory` directly without triggering the auto-push flow, even when `personal` has peers.

### Added

- **`src/scripts/fixes/` directory** — incremental, deployable bug-fix scripts that Claude runs on instruction to remediate issues on an installed peer. Each fix is idempotent, self-contained, and named `<version>-<sequence>-<description>.sh` for deterministic ordering. See [`src/scripts/fixes/README.md`](src/scripts/fixes/README.md) for the pattern. (WP-4 substrate)

### Fixed

- **Qdrant `$HOME` literal expansion bug** (script: [`src/scripts/fixes/0.5.0-001-fix-qdrant-home-path.sh`](src/scripts/fixes/0.5.0-001-fix-qdrant-home-path.sh)). Qdrant does NOT expand environment variables in YAML config; earlier qdrant-config.yaml used `storage_path: $HOME/...` which Qdrant interpreted literally, creating a directory named `$HOME` inside the install location. Functionally harmless but ugly and confusing for backup. The fix script: stops Qdrant, migrates the misplaced data tree to the intended location, cleans up the literal `$HOME` placeholder, patches the deployed YAML to use the absolute path, and restarts. Idempotent. (WP-4)

### Notes

- `/remember pull` semantics are unchanged. Pull is read-only inbound; the privacy invariant exists to protect outbound sharing.
- Daemon code is unchanged in this commit — the fix is purely in the Claude-facing skill rules.
- VERSION in `src/constellation.py` bumped from `0.4.0-alpha` to `0.5.0-alpha`. All v0.5 work targets the `v0.5` branch; `main` remains at the v0.4 line.
- The source `src/config/qdrant-config.yaml` still contains `$HOME` — fixing it there is part of WP-2 (install.sh), which will template the YAML the same way the launchd plist is currently templated. Until WP-2 lands, the WP-4 migration script is what fixes installed peers in place.

---

## [Unreleased] — v0.4.0 (current on `main`)

v0.4 is a routing-model simplification of v0.3 — same two-process architecture, same Qdrant collection, same MCP transport — but the dual-store / content-classification approach is replaced by **explicit group-keyed sharing**. Every memory carries a `groups: list[str]` payload; the default is `["personal"]` (local-only). Sharing happens when the user explicitly names a group. There is no longer any heuristic that tries to guess whether a memory should leave the machine.

Design doc: [`docs/v0.4_MEMORY_ARCHITECTURE.md`](docs/v0.4_MEMORY_ARCHITECTURE.md). 164/164 test invariants pass across the seven self-contained tests in `tests/`.

### Added

- **`groups: list[str]` payload field** on every memory — the routing key for sharing. Default `["personal"]`. A memory can belong to any number of groups simultaneously (multi-group membership), enabling cross-team sharing without duplicating storage.
- **`add_groups(memory_ids, groups)` MCP tool** — additively widens a memory's group set after the fact. Powers the "store now, share later" workflow: capture memories during work with `groups=["personal"]`, then later say *"share those with engineering"* and Claude calls `add_groups` followed by a targeted `group_push`. Additive only — un-sharing isn't supported (peers in a removed group already have the content).
- **`group_push(group, memory_ids?)` — optional `memory_ids` filter** for surgical pushes. Pass IDs for share-after-the-fact flows; omit for bulk push of every entry tagged with that group. Scope rule: push contacts only peers in the named group, even if the memory is also tagged for other groups.
- **Push-time group filter** — when shipping a memory to peer P, the wire-shape `groups` field is intersected with the set of groups P is a member of (from sender's view). The sender's `personal` tag is stripped on the wire — recipients never see a group they aren't part of.
- **Multi-membership configs** — Constellation's `memberships[]` accepts any number of entries. Every install gets a `personal` membership pre-populated with empty peers; users add teammate groups alongside.
- **Additive dedup-merge on receive** — `/memory/put` and `/pull` merge an incoming memory's `groups` into the existing local entry's `groups` when the `content_hash` matches. The local entry's group set never shrinks.
- **Three-tier node identity resolution** in `core.py`: `MEMFUSION_NODE_NAME` env var → `node_name` from Constellation's `config.json` → hostname fallback. Eliminates a class of install-time inconsistency bugs.
- **Backward-compat fallback** for v0.3 entries — entries lacking a `groups` field are read as `["personal"]`; v0.3 group entries (`source=group` + `group_name=X`) read as `[X]`. Optional one-shot migration runs at daemon startup.
- **Seventh test: `test-offline-rejoin.py`** — three peers (alice/bob/carol) come online in sequence with bob going offline mid-test; verifies peer-symmetric resilience (carol gets bob's memories via alice's relay).
- **Sixth test: `test-store-now-share-later.py`** — three peers (alice/bob/carol) exercise the canonical T1→T2→T3 user workflow from the design doc; verifies the scope rule (product push doesn't re-contact engineering peer) and push-time group filter.

### Changed

- **`/remember` skill rewrite** — drops v0.3's content-classification logic. Default scope is `personal` (local-only). Explicit `for <group>` syntax stores + pushes in one call. Natural-language *"share those with X"* maps to `add_groups` + targeted `group_push`.
- **Hooks default to `groups=["personal"]`** — Stop, PostToolUse:Write, and any future store-side hook tag captured memories as personal. The v0.3 risk of a hook auto-extracting a behavioral rule and propagating it via Constellation is structurally eliminated.
- **`store_memory` return shape** — `{status: stored|merged|duplicate, id, groups}`. Duplicate hits now return `id` (matching the existing entry) instead of `existing_id`. Merge happens when `content_hash` matches but the caller's groups widen the existing set.
- **`group_push` signature** — `{id}` → `{group, memory_ids?}`. Always scoped to one group per call; multi-group push is multiple calls from the caller. Breaking change vs. v0.3.
- **`group_pull` accepts optional `group` arg** — omit to iterate every configured group with peers; pass `group=<name>` to scope to one.
- **`/memory/put` wire shape** — receives `groups: list[str]` (already filtered by sender's push-time filter) instead of singular `group_name`. Validates that every incoming group is one this node is a member of.
- **`/memory/since`** — wire-shape carries `groups: [<requested_group>]` only; multi-group entries reconstruct on the caller's side via additive merge across separate pull calls.
- **`group_name` payload field deprecated** in writes. Reads still tolerate it via the v0.3 backward-compat fallback.

### Removed

- **`source=local` / `source=group` distinction** — there's one kind of entry now, distinguished only by which group(s) it belongs to. The `source` field is no longer written; reads tolerate legacy values via the fallback.
- **Single-membership constraint** on Constellation configs. v0.3 capped `memberships` at length 1; v0.4 lifts that.
- **Dual-routing classification logic** in `/remember`. There's no rule-vs-knowledge heuristic anymore; routing is by the explicit `groups` tag the user sets.
- **The file-mirror layer floated mid-design**. Briefly considered as a resilience backup; dropped because Qdrant's native snapshots cover durability without coordination cost.

### Fixed

- **`MEMFUSION_NODE_NAME` resolution** — `core.NODE_NAME` previously fell back to `socket.gethostname()` unconditionally. Now reads Constellation's `config.json` `node_name` field when available, eliminating divergence between the two processes' notion of peer identity.
- **`capture_file_write.sh` and `ingest_session.py`** — both hooks called a `srv.tool_store` function that never existed on `mem_fusion.py`'s module surface. Now call `core.store_memory` directly with explicit `groups=["personal"]`.

---

## [0.3.1] — 2026-05-13

Maintenance tag on the v0.3 line. Cuts the v0.3.x branch as the long-term-support fork; future bug fixes against the v0.3 routing model go there. `main` moves on to v0.4.

---

## [0.3.0] — 2026-05-12

Constellation lands as an opt-in companion to Mem-Fusion: peers share memory across machines and across teammates via HTTP fan-out. Mem-Fusion stays single-machine and fully local; Constellation handles the cross-peer traffic when installed. Purely additive vs. v0.1.0 — no breaking changes to existing tool names, hooks, or storage format. v0.1.0 users upgrade by re-pasting the updated `INSTALL_MEM_FUSION.md` prompt into Claude Code.

### Implementation updates since design lock

Implementation has refined several design assumptions captured in the original "Added / Changed / Architecture notes" entries below. The entries here supersede the older bullets where they conflict:

- **Single Qdrant collection model** — Mem-Fusion and Constellation share one collection (`cowork_memories`); federation entries are distinguished by a `source=federation` payload tag carrying `origin_node`, `group_name`, `received_at`. The earlier "disjoint canonical collection" design was dropped because tagging is simpler, makes the cowork-memory → mem-fusion upgrade a no-op (same datastore), and lets `core.search_recent` surface federation entries naturally via the `timestamp = received_at` alias.
- **Collection name is invariant** — `MEMFUSION_COLLECTION` env var and Constellation's `canonical_collection` config field both removed. The collection is always `cowork_memories` (the deployed cowork-memory name); peer isolation in dev comes from distinct Qdrant ports, not names.
- **`core.py` shared library** — extracted from `mem_fusion.py`; both daemons proxy to its 9 memory operations. `mem_fusion.py` is now a 133-line stdio MCP wrapper. Qdrant client is a module-level singleton, rebindable for tests that target multiple peer Qdrants in one process.
- **Mesh topology** — every Constellation peer is symmetric; "orchestrator" is a per-group role assigned to a peer at config time. Supersedes the earlier "designated orchestrator-only node" framing.
- **Constellation lives at `src/constellation.py`** — the `extensions/constellation/` location described in earlier bullets is obsolete.
- **Per-startup daemon log files** — each daemon startup creates `<LOG_DIR>/<component>.<UTC-timestamp>.log` (one file per process lifetime, `[<component>]` line prefix preserved for cross-daemon catenation). Default `LOG_DIR` is `~/.local/share/mem-fusion/logs/`; override via `MEMFUSION_LOG_DIR`.
- **`tests/` directory** — `dev/` renamed to `tests/`; split into `tests/constellation/` (multi-peer federation, requires dev peer Qdrants on `:6433/:6533/:6633`) and `tests/mem_fusion/` (single-node MCP integration with self-managed Qdrant on `:6733`). Peer-management scripts (`setup-peer.sh`, `start-peer.sh`, etc.) sit at `tests/` root.
- **`tests/mem_fusion/test-mcp-tools.py`** — single self-contained MCP integration test (30 invariants): spins up its own Qdrant in a tempdir, spawns `mem_fusion.py` over stdio JSON-RPC, exercises all 9 tools end-to-end. No peer setup required.
- **Constellation peer tests use `core.py` for peer-side data access** — previously raw `QdrantClient` calls; now rebind `core.qdrant` per peer and invoke `core.store_memory()`, `core.export_record()`. Tests exercise the production code path.

### Removed since design lock

- **Ollama-offline fallback queue** — failures now surface as `{"error": "ollama_unreachable", ...}` rather than silently queueing. Local Ollama down → Mem-Fusion stops working visibly, by design.

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

[Unreleased]: https://github.com/muycoreano/mem-fusion/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/muycoreano/mem-fusion/releases/tag/v0.3.1
[0.3.0]: https://github.com/muycoreano/mem-fusion/compare/v0.1.0...v0.3.0
[0.1.0]: https://github.com/muycoreano/mem-fusion/releases/tag/v0.1.0
