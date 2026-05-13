# Mem-Fusion v0.3.0 — Architecture

**Status:** MVP architecture, current as of 2026-05-12
**Companion to:** [`CONSTELLATION_ARCHITECTURE.md`](CONSTELLATION_ARCHITECTURE.md)
**Repository:** [github.com/muycoreano/mem-fusion](https://github.com/muycoreano/mem-fusion)

---

## 1. Mission

**Mem-Fusion gives Claude Code long-term, semantically-searchable memory on one machine.** Out of the box Claude Code has only a context window — every session starts blank, decisions evaporate, preferences must be re-stated, resolved errors recur. Mem-Fusion fuses memory across two axes:

- **Across sessions** — Tuesday's decision surfaces in Friday's session, automatically.
- **Across cognitive layers** — episodic, semantic, procedural, and working memory unified into one substrate via 9 MCP tools and 4 automatic Claude Code hooks.

Mem-Fusion is the *personal memory* foundation. The optional Constellation extension (separate daemon, same Qdrant collection, same machine) adds the *across machines* axis for group-shared memory.

---

## 2. Two memory layers

Mem-Fusion ships with two memory stores that serve different purposes. They are not alternatives; they are complementary.

| Layer | Purpose | Access pattern | User-managed |
|---|---|---|---|
| **Vector memory** (Qdrant + Ollama embeddings) | Episodic and semantic memory — decisions, context, preferences, errors, references. Searchable by semantic similarity. | On-demand via `search_memory(...)` and automatically by the UserPromptSubmit hook. | Mostly automatic (via hooks + `/remember`); MCP tools allow manual curation. |
| **File-based memory** (`~/.claude/projects/.../memory/` tree) | Behavioral rules, always-loaded context, durable instructions. Loaded at session start by Claude's built-in mechanism. | Loaded at session start; not searched. | User-curated; rules are explicit. |

**Vector memory** is what Constellation shares across machines. **File-based memory** stays per-machine, personal, and is never sent to peers — rules and personal preferences shouldn't auto-sync to teammates' Claudes.

The rest of this document describes the vector memory layer, the MCP server, and the four hooks. File-based memory is a Claude Code feature; Mem-Fusion is the vector-memory implementation.

---

## 3. Stack

| Component | Role | Process model |
|---|---|---|
| **Qdrant 1.13.4** | Vector database (port 6333) | launchd-managed (`com.branchapp.memfusion.qdrant`); persistent |
| **Ollama 0.20.5 + `nomic-embed-text`** | 768-dim embeddings (port 11434) | launchd-managed (`com.branchapp.memfusion.ollama`); persistent |
| **`mem_fusion.py`** | MCP server (9 tools, stdio transport) | Spawned by Claude Code per session; ephemeral |
| **4 Claude Code hooks** | Automatic memory capture + context injection | Lifecycle scoped to Claude Code session |
| **`/remember` skill** | User-curated `importance=5` storage | On-demand |

Everything is localhost. No network exposure. ~200 MB disk footprint.

---

## 4. Memory object model

Each memory in the vector store is a single Qdrant point with:

```
id            UUID (assigned at store time)
vector        768 floats (Ollama embedding of content)
payload:
  content       string (the memory text, verbatim)
  content_hash  string (SHA256(content.strip().lower())[:16] — dedup key)
  type          enum (decision | fact | preference | error | code | context | session)
  tags          array of strings (optional topic tags)
  project       string (optional multi-tenant label)
  importance    int (1 = trivial, 5 = user-curated)
  session_id    string (optional, links memories to a Claude session)
  timestamp     ISO-8601 UTC (microsecond precision)
  source        string (e.g., "hook", "manual", "preload", "constellation-pull")
```

**Critical invariant: content_hash is the dedup key, computed identically across Mem-Fusion and Constellation.** Different identifier representations of the same content collide on the same hash; users don't get duplicate memory entries from re-issuing the same text.

---

## 5. Memory types

Seven types, each with distinct retention and retrieval semantics:

| Type | Captures |
|---|---|
| `decision` | A choice made (architecture, tool selection, approach, scope) |
| `preference` | User-stated rules ("always X", "never Y") |
| `fact` | Verifiable observations about the project or system |
| `error` | Resolved errors and their fixes |
| `code` | Significant code patterns or file writes (≥100 lines, auto-captured) |
| `context` | Background context about an initiative |
| `session` | Auto-extracted summaries of Claude sessions |

**Importance scale (1–5):** 1 = trivial; 3 = normal (default for store_memory); 4 = important (typical for decisions/preferences); 5 = user-curated (only via the `/remember` skill).

---

## 6. The 9 MCP tools

All tools operate against the local collection (`mem_fusion_memories` by default; overridable via `MEMFUSION_COLLECTION` env var).

| Tool | Purpose |
|---|---|
| `store_memory(content, type, tags?, project?, importance?, session_id?)` | Store a new memory. Embeds via Ollama, computes content_hash, dedups, inserts. Returns `{id, status}` where status ∈ {stored, duplicate}. |
| `search_memory(query, top_k?, project?, type?, since?, min_importance?)` | Semantic search via embedding the query and Qdrant cosine similarity. Scores >0.75 are typically meaningful. |
| `search_recent(hours?, top_k?, project?, type?)` | Time-filtered search — no vector needed. For "what happened recently in project X" probes. |
| `upsert_memory(id, content?, tags?, importance?, ...)` | Update an existing memory by ID. Re-embeds if content changes. |
| `find_or_create(content, type, ...)` | Search for similar content first; store if nothing relevant exists. The "soft store" — avoids duplicates without exact-content collision. |
| `delete_memory(id)` | Remove a memory by ID. |
| `get_related(reference_id, top_k?)` | Find memories semantically similar to a given memory ID. |
| `memory_stats()` | Return summary statistics — total count, breakdown by type/project, most-recent timestamp. |
| `export_record(id)` | Return a stored memory's full Qdrant record including its 768-dim vector. Used by Constellation to propagate memory across machines verbatim (no re-embedding). |

`export_record` is the single bridge from Mem-Fusion to Constellation: any extension that needs faithful memory propagation can extract a complete record without re-running the embedding model.

---

## 7. Four hooks make memory invisible

Claude Code's hook system runs scripts at specific lifecycle events. Mem-Fusion wires four hooks into `~/.claude/settings.json`:

### 7a. SessionStart — context priming

Fires at the start of every Claude Code session. Calls `tool_search_recent({hours: 48, top_k: 5})` plus `tool_stats({})` and injects a `<memfusion_status>` block into Claude's first message so Claude sees:

- Total memories stored
- Last stored timestamp
- Recent activity preview (last 48h)
- A reminder to use `search_memory(query=...)` to retrieve more

Claude can act on this context immediately — referencing relevant prior work, surfacing pending state, etc.

### 7b. UserPromptSubmit — relevant-memory injection

Fires on every user message ≥15 chars. Calls `tool_search({query: <user_prompt>, top_k: 5, min_importance: 2})` and injects memories scoring ≥0.75 wrapped in `<memory_context>` blocks before Claude responds. Deduplicated per session — the same memory won't be re-injected if the user references it again.

Effect: Claude has the most relevant past context for every prompt, without the user having to explicitly recall.

### 7c. PostToolUse:Write — code-memory capture

Fires after Claude writes a file. If the file is ≥100 lines, captures a `code`-type memory in the background containing the file path, line count, project, and the first 10 lines as a header preview. Fire-and-forget; doesn't block Claude.

### 7d. Stop — session ingestion

Fires when Claude's session ends. Reads the latest `.claude/sessions/*.jsonl` transcript and extracts:
- Decision patterns (`we decided`, `going with`, `chose to`)
- Error resolution patterns (`fixed`, `resolved`, `root cause`)
- Preference patterns (`always`, `never`, `prefer`)

Stores extracted signals as memories of the appropriate type, plus one `session`-type summary of the conversation. Background process; doesn't block exit. Deduped by session ID so the same session isn't ingested twice.

---

## 8. Process architecture

```
                          ┌──────────────────────────┐
                          │   Claude Code session    │
                          │   (user-facing CLI)      │
                          └────┬─────────────────────┘
                               │ spawns
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Mem-Fusion MCP server                          │
│                    (mem_fusion.py — stdio)                        │
│                                                                   │
│   ┌──────────────────────────────────────────────┐               │
│   │ 9 MCP tools (store, search, ...)             │               │
│   └────┬─────────────────────────────────────────┘               │
│        │                                                          │
│        ▼ HTTP localhost                                           │
└──────┬────────────────────────────────────────────────────────────┘
       │
       ├──► Qdrant (localhost:6333)
       │    └── collection: mem_fusion_memories
       │        ├── 768-dim vectors (cosine)
       │        └── payload indexes: type, project, source,
       │                              session_id, content_hash,
       │                              importance, timestamp
       │
       └──► Ollama (localhost:11434)
            └── nomic-embed-text model

  ┌───────────────────────────────────────────────────┐
  │ launchd-managed daemons (persistent)              │
  │ ├── com.branchapp.memfusion.qdrant                │
  │ └── com.branchapp.memfusion.ollama                │
  └───────────────────────────────────────────────────┘

  ┌───────────────────────────────────────────────────┐
  │ Hooks wired into ~/.claude/settings.json:         │
  │ ├── SessionStart        → session_prime.sh        │
  │ ├── UserPromptSubmit    → prompt_memory_inject.sh │
  │ ├── PostToolUse:Write   → capture_file_write.sh   │
  │ └── Stop                → ingest_session.py       │
  └───────────────────────────────────────────────────┘
```

**Key property:** The MCP server is **ephemeral** — Claude Code spawns it for each session via stdio transport, and it exits when the session ends. The Qdrant and Ollama daemons are **persistent** — they hold the state across sessions. This means a Mem-Fusion install is mostly stateless on the MCP-server side; the source of truth is Qdrant.

---

## 9. Storage layout on disk

```
~/.local/share/mem-fusion/
├── bin/qdrant                        # Qdrant binary
├── venv/                             # Python venv (mcp, qdrant-client, httpx pinned)
├── mem_fusion.py                     # MCP server (copied from src/ during install)
├── qdrant-config.yaml                # Qdrant config (storage paths, ports)
├── requirements.txt                  # pinned Python deps
├── scripts/
│   ├── init_collection.py            # one-time collection bootstrap
│   ├── session_prime.sh              # SessionStart hook
│   ├── prompt_memory_inject.sh       # UserPromptSubmit hook
│   ├── capture_file_write.sh         # PostToolUse:Write hook
│   ├── ingest_session.py             # Stop hook
│   ├── wire_hooks.py                 # settings.json merger (install-time)
│   └── smoke-test.sh                 # post-install verification
├── qdrant-data/                      # Qdrant storage (vectors + payload + indexes)
├── snapshots/                        # Qdrant snapshot dir
├── logs/                             # mcp.log, qdrant.log, ollama.log, ingest.log
└── queue/                            # queue dir for offline writes
                                       # (used when Ollama is unreachable; also
                                       #  Constellation's promotion queue lives here
                                       #  if Constellation is installed)
```

Qdrant collection schema (created by `init_collection.py`):

```
collection: mem_fusion_memories
  vectors:        768-dim, cosine distance
  payload indexes:
    type           keyword
    project        keyword
    source         keyword
    session_id     keyword
    content_hash   keyword       # primary dedup index
    importance     integer       # range filters for min_importance
    timestamp      datetime      # range filters for `since=...`
```

The collection name is `MEMFUSION_COLLECTION` env-var overridable; defaults to `mem_fusion_memories`. Tests and dev peers use distinct collection names (e.g., `mem_fusion_dev_memories`, `mem_fusion_peer_b_memories`) so multiple Mem-Fusion installs can coexist on one Qdrant instance.

---

## 10. Memory lifecycle

### Store

```
User says "/remember <something>"     OR     Hook fires (PostToolUse:Write, Stop)
        │                                                │
        ▼                                                ▼
   /remember skill                                  hook script
   calls store_memory(...)                          calls store_memory(...)
        │
        ▼
   mem_fusion.py:
     1. Compute content_hash
     2. Check for existing memory with same content_hash
        → if found: return {status: "duplicate", existing_id}
     3. Embed content via Ollama
        → if Ollama unreachable: queue write to ~/.local/share/mem-fusion/queue/
                                  return {status: "queued"}
     4. Generate UUID
     5. Qdrant upsert
     6. Return {status: "stored", id}
```

### Search

```
User prompt enters Claude session     OR     Claude calls search_memory(...)
        │                                                │
        ▼                                                ▼
   UserPromptSubmit hook                            (direct MCP call)
   embeds prompt + searches
        │
        ▼
   mem_fusion.py.tool_search:
     1. Embed query via Ollama
     2. Qdrant cosine-similarity search (top_k, with filters)
     3. Format results: {id, score, content, type, project, tags, importance, timestamp}
     4. Inject scores ≥0.75 above Claude's response context
```

### Dedup

`content_hash = SHA256(content.strip().lower())[:16]` — the **same function** as Constellation's `content_hash`. This means:

- Two memories with identical content (modulo whitespace + case) collide and the second is rejected.
- A memory stored on peer-b that gets promoted to a Constellation canonical can be detected as already-known on peer-c if peer-c happens to store the same content locally.
- Whitespace and case insensitivity catch the most common forms of "I typed it slightly differently this time" friction.

The 16-character hex prefix is sufficient for billions-of-memories collision resistance — 64 bits of hash space; even a billion entries gives ~10⁻¹¹ collision probability.

### Queue (offline fallback)

If Ollama is unreachable when `store_memory` is called, the memory is written as JSON to `~/.local/share/mem-fusion/queue/<timestamp>-<content_hash>.json`. The queue is drained on next successful embedding (no separate drain process — opportunistic). This is mem-fusion's own offline-write queue, separate from Constellation's promotion queue.

---

## 11. Relationship to Constellation

Constellation is an **optional companion daemon** for group memory sharing. It's bundled in the same repo (`src/constellation.py`) but runs as a separate process with its own MCP server and its own network endpoint.

| Aspect | Mem-Fusion | Constellation |
|---|---|---|
| Purpose | Personal memory (one machine, one user) | Group memory (across machines) |
| Transport | stdio MCP, ephemeral per session | HTTP MCP, persistent daemon |
| Network exposure | Localhost only | Network-bound for peer-to-peer reachability |
| Qdrant collection | `cowork_memories` | `cowork_memories` (same collection; entries tagged `source=group`) |
| Lifecycle | Per-Claude-session subprocess | launchd-managed always-on |
| Required? | Yes (base install) | No — optional add-on |

The two daemons share the same Qdrant collection but write entries with distinct `source` tags (`local` vs. `group`). Process isolation is what keeps them separate: Constellation has no privileged access to Mem-Fusion's in-memory state, and the only path from "remote peer makes a request" to "local memory store" goes through Constellation inserting a new row tagged `source=group`.

The bridge between the two: `export_record(id)` in Mem-Fusion returns a complete Qdrant record (with vector) for a given memory. Constellation uses this to send a local memory to peers in a group — the vector is copied verbatim, no re-embedding, so semantic equivalence is preserved when the memory arrives at the other end.

**Critical safety property:** the `content_hash` function is **byte-identical** in Mem-Fusion and Constellation. This means a memory stored locally and then sent to peers has the same hash in every peer's store, enabling dedup and integrity verification.

---

## 12. Content-addressed verification

Every memory carries its `content_hash` in the payload. Anyone with the content can recompute the hash and verify it matches. This is the integrity invariant when memories cross trust boundaries (i.e., into Constellation).

Computation:
```python
def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]
```

**Why strip + lower:** to catch the trivial-difference duplication case ("Hello world" vs "hello world  " vs "  Hello World  "). Semantically identical content should produce the same hash.

**Why 16 hex chars (64 bits):** sufficient collision resistance for the realistic ceiling (≤10⁹ memories), short enough to fit in URLs / logs / debug output without visual noise.

The hash is **not** a cryptographic identifier in the sense of "globally unique cross-organization." Two unrelated installs could (in principle) collide on identical content — by design, since dedup is the goal.

---

## 13. Storage layout in `~/.claude/`

Mem-Fusion writes to two locations outside its own install directory:

```
~/.claude/
├── settings.json              # 4 hook entries injected by wire_hooks.py
└── skills/remember/SKILL.md   # /remember skill definition
```

Both are idempotently merged (settings.json) or copied (SKILL.md) during install. The install script backs up `settings.json` before any edit (`settings.json.bak.<timestamp>`).

---

## 14. MVP scope

The MVP that ships in v0.3.0 covers:

- ✅ All 9 MCP tools
- ✅ All 4 Claude Code hooks
- ✅ The `/remember` skill (importance=5)
- ✅ Local-only operation (no telemetry, no cloud)
- ✅ macOS launchd integration
- ✅ Constellation-compatible `export_record` tool
- ✅ Build-from-source distribution model (`build_install.sh` packager → paste-into-Claude `INSTALL_*.md` artifacts)

Optionally: Constellation extension adds group memory sharing on top. See [`CONSTELLATION_ARCHITECTURE.md`](CONSTELLATION_ARCHITECTURE.md).

---

## 15. What Mem-Fusion is NOT

- **Not cross-platform.** macOS only (launchd is hard-required by the current architecture).
- **Not multi-user.** Single user per install. Multi-user systems would need a separate Qdrant + Ollama deployment per user.
- **Not encrypted at rest.** Qdrant storage is plaintext on disk. The threat model is "trust your local filesystem."
- **Not a database.** Mem-Fusion is a memory layer for Claude. It doesn't expose direct SQL/Qdrant access to applications; everything flows through the MCP tool surface.
- **Not a cloud service.** Localhost only. No data leaves the machine without explicit user action (Constellation does, but only to nodes the user has configured).
- **Not a replication system.** Even with Constellation, peers selectively send writes to each other; they don't replicate the whole store. Each peer's Qdrant is its own source of truth.
- **Not a substitute for file-based MEMORY.** Vector memory is for searchable episodic/semantic content. Behavioral rules belong in the file-based MEMORY tree where they're always loaded.

---

## 16. Open architectural concerns

Items where the current design has known limits, surfaced for future versions:

- **Hook installation is global.** The 4 hooks fire on every Claude Code session, not per-project. A user wanting "different memory stores per project" would need a heavier multi-collection setup.
- **No explicit memory deletion lifecycle.** `delete_memory` exists but there's no GC or expiry. Memories accumulate forever unless manually deleted.
- **No conflict resolution for `upsert_memory`.** Last-write-wins. Two concurrent upserts can lose data; the use case is rare in single-user/single-session contexts.
- **No multi-machine sync of personal memory.** Each Mem-Fusion install is its own canonical. Constellation handles group memory; personal memory across machines (for the same user) is not addressed.
- **Embedding model is fixed at install time.** Changing models requires re-embedding everything. No protocol for cross-model migrations.

These are real limitations but not blockers for MVP. They'll be addressed as real usage surfaces specific needs.
