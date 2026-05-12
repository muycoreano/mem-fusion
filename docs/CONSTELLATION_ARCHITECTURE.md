# Constellation v0.3.0 — Architecture (Draft)

**Status:** DRAFT — design locked, implementation in progress
**Date:** 2026-05-10
**Companion to:** Mem-Fusion v0.1.0+ ([github.com/muycoreano/mem-fusion](https://github.com/muycoreano/mem-fusion))

---

## 1. Mission

**Constellation enables group memory across multiple Mem-Fusion nodes.** Where Mem-Fusion gives Claude long-term memory across sessions on one machine, Constellation lets that memory federate across machines — sharing curated knowledge between team members' AI agents while preserving each member's personal memory privacy.

Constellation is bundled with Mem-Fusion (same repo at `src/`) but runs as a **separate daemon process** with its own HTTP server. Both daemons are clients of a shared `core_mem_fusion` library that operates on a single Qdrant collection per peer — see §7 Process Architecture. The two daemons serve different audiences:

| | Mem-Fusion (`mem_fusion.py`) | Constellation (`constellation.py`) |
|---|---|---|
| Audience | Claude Code on this machine | Other peers on the network |
| Transport | stdio MCP, spawned per Claude session | HTTP server + SSE pub/sub, persistent daemon |
| Network exposure | Localhost only | Network-bound for inter-peer reachability |
| Storage | Shared single Qdrant collection (`mem_fusion_memories`) via `core_mem_fusion` | Same shared single Qdrant collection via `core_mem_fusion` |
| Lifecycle | Ephemeral (Claude subprocess) | Always-on (launchd-managed) |

Both daemons read and write the same Qdrant collection through `core_mem_fusion`. They distinguish memory provenance by `source` tag (`local` vs `federation`) and `origin_node` (the author identity). There is no IPC between the two daemons; their shared storage layer is the rendezvous.

**Critical safety property:** Process isolation prevents network-side requests from reaching Claude's stdio MCP surface. A compromised constellation can affect only what the federation protocol exposes; it cannot escalate to invoking Claude's personal-memory MCP tools directly. Storage isolation is enforced by `source` tags within the single collection, and by network exposure rules (`mem_fusion.py` is unreachable from the network at all).

---

## 2. Primary use cases

Mem-Fusion + Constellation serves two scenarios. The first is the dominant case the system is designed around; the second is the natural extension that uses the same architecture without modification.

### 2.1 Primary use case — one user, multiple machines

The most common scenario: **a single developer using Claude Code on more than one machine.** A laptop and a desktop. A work machine and a home machine. A primary workstation and a server they SSH into. Without federation, every machine is its own memory island — what Claude learns on the laptop is invisible to Claude on the desktop. The user has to manually re-establish context every time they switch machines.

Constellation closes this gap. The user''s own machines form a small group (typically 2-4 peers). Memories stored on any one machine propagate to the others automatically. The user gets one continuous Claude memory across all the devices they work from.

**Why this is the primary case:**

- Most developers work across multiple devices already (laptop ↔ desktop is the default; remote development is increasingly common)
- It''s the direct multi-device extension of Mem-Fusion''s value proposition (long-term memory across sessions → long-term memory across machines)
- It solves a pain point every multi-device user has experienced
- It validates the federation architecture end-to-end without needing collaboration semantics
- It''s the lowest-friction adoption path: no team coordination, no permission negotiation, no "convince colleagues to install something" friction
- Privacy concerns are trivial in this case — it''s all one person''s memory; everything is "shared" with themselves by definition

**Architectural shape for this case:**

- The user''s machines form a Constellation group named for the user (e.g., `mitch@personal` or `self@dev-machines`)
- Each machine runs the standard install: Mem-Fusion + Constellation, same software stack
- The group''s peer list is the user''s machines
- Configurations are identical across machines except for `node_name` and `listen_address`
- New machines join by adding them to the peer list and restarting Constellation on existing peers — no migration, no coordination, no central authority

### 2.2 Secondary use case — small team, shared knowledge

The same architecture extends to teams: multiple users, each with one or more machines, all participating in a shared group. Bounded at approximately ten peers per group at MVP scale (per the mesh-topology cost analysis in §4).

The protocol doesn''t distinguish between the two cases. A "group" is just a named set of peers; whether those peers all belong to one person or to many, the wire protocol and storage model are identical.

**The natural progression from single-user to team:**

1. Solo: one user, one machine. Personal Mem-Fusion only. No Constellation needed.
2. Multi-device: same user, second machine added. Set up Constellation on both, each lists the other in the peers config. Memory syncs across the user''s two machines.
3. Team: invite a colleague. Add their machine to the peer list (and they add yours to theirs). The colleague''s memories now appear in the group; the user''s memories appear in the colleague''s group. Up to ~10 peers, the cost stays trivial.

Same install procedure at every step. Same config schema. Adding the third machine (whether your own or a colleague''s) is the same operation. The "team" case is just "more peers in the group."

### 2.3 What changes between the use cases

| Concern | Single user (primary) | Team (secondary) |
|---|---|---|
| Privacy of personal vs shared | Trivial — everything is the user''s; no distinction needed in practice | Real — the `source=local` vs `source=federation` distinction matters; users want to know what came from team vs themselves |
| Trust model | User trusts their own machines | Each member trusts every other member of the group equally |
| Setup ceremony | One person edits configs on their own machines | A "group setup coordinator" picks the group name + gathers peer URLs + distributes config |
| Author attribution (`origin_node`) | "Which of my machines first stored this" — useful for debugging | "Which colleague wrote this" — critical for context |
| Typical mesh size | 2–4 | Up to ~10 |
| Concurrent-write conflicts | Rare (single user, occasionally same content from two devices) | Slightly higher (multiple users may store similar content) — content_hash dedup handles both cases |

The architecture handles both use cases with the same mesh + content_hash dedup + source-tagged storage. No code paths differ based on which case is in play.

### 2.4 Why this framing matters for product positioning

The single-user multi-machine case is the most common scenario and the lowest-friction adoption path. Treating it as primary in the documentation and onboarding has consequences:

- The first install story is framed as "you, on multiple machines" not "you, on a team"
- Group setup is presented as something a single user can do alone (no need to convince colleagues)
- The team case is positioned as "naturally extends from this" rather than the main mental model
- Marketing and copy emphasize "your memory across your devices" before "shared team knowledge"

The team case remains supported — it''s the same architecture — but it''s the secondary mental model rather than the primary one. This positioning carries through the install procedure, the README, and any onboarding flows.

---

## 3. Object Model

Two entities. No special roles.

### Node

A participant in the federation. Each Mem-Fusion install on a machine corresponds to one node. Every node is **symmetric** — there are no orchestrators, no peers-with-fewer-capabilities, no central coordinators. Every node holds its own local store and can both publish and subscribe.

```
node:
  name             string         e.g., "alice-mac"
  endpoint         URL            e.g., "http://10.0.0.5:7533"
  memberships      list of group memberships (group_name + peers in that group)
```

### Group

A named federation of symmetric nodes. The group is the unit of memory scope.

```
group:
  name             string         e.g., "engineering@branch"
  peers            list of Node   the full mesh — every node knows every other node
```

There is no "orchestrating node." Every node in a group has the same role and the same capabilities. Group state is the union of every member's local store, reconciled via mesh synchronization (§6).

### Why mesh, not orchestrator-centric

The MVP target is small teams (≤10 people per group) on a local network. At that scale, the cost of every peer holding the group's state directly is trivial (a few MB), and the resilience properties are excellent: any peer can serve any other peer's catch-up query, no single failure point can stall the group, and group setup is one symmetric install per member.

The orchestrator-centric design (one designated node holds the canonical, others are clients) makes sense at larger scales where bandwidth, storage cost, and operational management justify the centralization. That is reserved for the **enterprise tier** (§14). The MVP is mesh.

### What this model dissolves

There are no "brokers," "relays," "bridges," "federation servers," "canonical stores held by privileged nodes." Those concepts collapse into:
- A node (the actor — every node is fully capable)
- A group (the federation unit — a named set of nodes)
- A membership (the relationship — this node belongs to that group)

Cross-group flow happens only when a node has memberships in multiple groups and makes explicit, deliberate calls. No third-party intermediation.

---

## 4. The Routing Rules

Four invariants. Every protocol operation respects them.

### Rule 1: Mesh fan-out, no relays

A node storing a memory in a group fans out the memory directly to every other peer in the group. No intermediation, no path discovery. Each delivery is a one-hop call from origin to recipient.

### Rule 2: Receivers do NOT re-fan-out

When peer-B receives a memory from peer-A, peer-B stores it locally and **does not** re-broadcast it. Fan-out is the originator's responsibility. Without this rule, every store would produce N² messages. Combined with content_hash dedup at receivers, this keeps mesh traffic at O(N) per store.

### Rule 3: Membership equals authorization

A node can only fan out memory to (or query memory from) groups it is a member of. Membership IS the permission for v0.3.0 MVP. Cryptographic authentication is part of the enterprise tier (§14).

### Rule 4: No automatic cross-group propagation

A node with memberships in multiple groups does NOT automatically propagate memories from one group to another. Cross-group flow requires an explicit second store_memory call by the node, deliberately scoped to the second group. Provenance is recorded in the new copy.

### Consequences

- **Routing is degenerate.** Each store produces N-1 fan-out calls plus one local insert. No routing protocol because there is no routing problem.
- **Bandwidth scales linearly with group size.** For ≤10 peers, fan-out cost per store is at most 9 outbound calls — trivial. Beyond that, the enterprise tier (§14) introduces hub-and-spoke topology to keep per-store cost bounded.
- **Trust surface is the group's peer list.** Each node trusts every other peer in its group equally. There is no privileged "orchestrator" to compromise; correspondingly, there is no central trust anchor.
- **Audit trail via `origin_node` tag.** Every stored memory records its original author. Even after fan-out + dedup at receivers, the author identity is preserved.

---

## 5. Protocol Surface

Six HTTP endpoints. All scoped by group. Every peer exposes all of them — there are no orchestrator-only or peer-only endpoints.

### `POST /memory/put`

Submit a memory to a peer's local store. Used by mesh fan-out: when peer-A stores a memory locally, it POSTs to every other peer in the group.

```
inputs:
  group_name      string       scope
  memory_record   object       full record: content, vector, content_hash, type, tags, importance, original_timestamp
  provenance      object       origin_node, origin_local_id, submitted_at, submission_kind

returns:
  status:        stored | duplicate
  canonical_id:  UUID assigned by this peer (the receiver)
  received_at:   ISO-8601 microsecond timestamp (this peer's clock)
```

Authorization: caller's membership in `group_name` is implicit (no auth in MVP; local-network-only deployment).

### `GET /memory/get`

Fetch one or more memories from a peer's local store. Used for catch-up via pull cycle.

```
query params:
  group_name      string             scope
  id              optional string    specific memory by canonical_id
  since           optional ISO       filter received_at >= since; sorted ascending
  limit           optional int       default 10

returns:
  count:     int
  memories:  list of full memory records
```

### `GET /memory/since`

Lightweight count probe. Used by the pull cycle to decide whether a follow-up `/memory/get` is needed without paying for the full payload.

```
query params:
  group_name      string             scope
  since           optional ISO       filter received_at > since; returns 0 if absent and group empty

returns:
  group_name:           string
  since:                ISO | null
  count:                int
  latest_received_at:   ISO | null
```

### `GET /memory/events`

**Server-Sent Events (SSE) stream** of `memory.put` events as they happen on this peer. Subscribers connect and hold the connection open; the peer pushes events down as new local memories land.

```
query params:
  group_name      string             scope

headers (subscriber):
  Last-Event-ID   optional ISO       resume from this received_at; absent means replay full history

response:
  Content-Type: text/event-stream
  (long-lived; events streamed as they occur)

event format:
  id: <received_at_iso>
  event: memory.put
  data: <full memory record JSON>
```

Reconnect semantics: subscribers send `Last-Event-ID` on reconnect to resume from their last-applied cursor. The publisher replays from its local store filtered by `received_at > cursor`.

### `GET /peers`

Return the directory of peers known to this node for a given group, with their submission counts and last-seen timestamps (derived from received memories).

### `GET /peers/self`

Return this node's identity — `node_name`, `listen_address`, `version`, configured `memberships`.

### `GET /health`

Liveness probe. Returns `{ok: true, daemon, version, node_name, memberships}`. No authorization.

### What is NOT in the protocol

- **No `subscribe` / `unsubscribe` separate verbs.** SSE subscription is implicit in `GET /memory/events`; disconnect ends it.
- **No callback-URL webhooks.** Subscribers come to the publisher (SSE), not the other way around. Avoids inbound-listener requirements on subscribers.
- **No `memory/delete` or `memory/update`.** MVP is append-only.
- **No `broker_request` / cross-group routing.** Cross-group flow is the responsibility of a multi-membership node making explicit per-group calls.
- **No authentication tokens.** MVP relies on the local-network deployment as the trust boundary. Auth is part of the enterprise tier (§14).

---

## 6. Synchronization

How memories propagate across mesh peers. **SSE-primary with a polling safety net.** No persistent queues, no orchestrator-centric pull, no callback subscriptions in the v0.3.0 webhook sense — just long-lived SSE streams between every peer pair, with a slow background pull cycle as belt-and-suspenders.

### 5.1 The model in one paragraph

Every peer holds its own local store. When peer-A stores a memory, it fans out via SSE to every other peer in the group. Each receiver dedups by content_hash and stores locally with `source=federation` and `origin_node=peer-A`. Receivers do NOT re-fan-out. Subscribers reconnect on drop via `Last-Event-ID`. A periodic pull cycle catches any drift between peers. Convergence is eventual; the only durable state is each peer''s Qdrant collection.

### 5.2 The two transports

| Channel | Wire shape | Latency | Role |
|---|---|---|---|
| **SSE pub/sub** *(primary)* | Each peer exposes `GET /memory/events?group_name=X` as a long-lived `text/event-stream` connection. Every other peer in the group holds an open connection to it. New events stream down as they happen. | Sub-second when both peers online; sub-second on reconnect via `Last-Event-ID` | Real-time delivery; primary path |
| **Pull cycle** *(safety net)* | Every 60 seconds, each peer calls `GET /memory/since?group_name=X&since=<cursor>` on every other peer; if anything new, pulls via `GET /memory/get?group_name=X&since=<cursor>` | Up to 60s | Belt-and-suspenders; catches any SSE drift |

The pull cycle exists to validate that SSE is working correctly in production. After we''ve accumulated evidence that SSE is reliable, the pull cycle can be dropped in a future version.

### 5.3 Single Qdrant collection per peer, source-tagged

Every peer has **one** Qdrant collection (default `mem_fusion_memories`). Entries are tagged by source:

| Tag value | Meaning |
|---|---|
| `source=local` | Stored by this peer''s own user (via Claude `store_memory` or `/remember`) |
| `source=federation` | Received from another peer via SSE fan-out or pull cycle |

Federation entries additionally carry `origin_node` (the original author''s node name, carried through fan-out + pull) and `group_name`. Search across the collection naturally returns both personal and group-shared memories; the user can filter by `source` if desired.

### 5.4 Cursor model

Each peer tracks a **per-peer cursor** for catch-up: "the latest `received_at` timestamp I''ve observed from this remote peer." Cursors are microsecond-precision ISO-8601 timestamps in the remote peer''s clock. No global ordering, no clock-skew comparisons; each peer maintains a view of every other peer''s log position.

The cursor is consumed in two places:
- As the `Last-Event-ID` header on SSE reconnect
- As the `since=` parameter on the pull cycle''s `/memory/since` and `/memory/get` calls

Cursor advance rule: **after apply, not after read**. If a batch of memories arrives but only some are successfully stored locally, the cursor advances only to the last successfully-applied entry''s `received_at`. Re-fetching on the next cycle costs a few dedup'd inserts; missing an entry costs convergence.

### 5.5 Fan-out (publisher side)

When the user stores a memory via Claude on peer-A:

1. mem_fusion.py calls `core_mem_fusion.store_memory(...)` which inserts into peer-A''s Qdrant with `source=local`.
2. constellation.py, polling `core.get_new_entries_since(cursor, source_filter="local")` every ~1 second, detects the new entry.
3. constellation.py broadcasts the entry as an SSE event to every currently-connected subscriber (peer-B, peer-C, …).
4. Each SSE event carries the full memory record (content + 768-dim vector + payload + provenance) with `id: <received_at_iso>` and `event: memory.put`.

If a subscriber connection is currently down, the event is **not** queued — the subscriber will resume on reconnect with `Last-Event-ID`, and the pull cycle catches anything missed.

### 5.6 SSE subscription (subscriber side)

Each peer establishes one outbound SSE subscription to every other peer in the group. The subscriber:

1. Opens `GET /memory/events?group_name=X` with `Last-Event-ID: <cursor>` header.
2. Reads `text/event-stream` lines as they arrive, parses each event.
3. On `event: memory.put` with full record payload, calls `core.store_memory(..., source="federation", origin_node=<from event>)` to insert into the local collection. Content_hash dedup means a memory already known via another path becomes a no-op.
4. Advances the per-peer cursor to the just-applied `received_at`.
5. On disconnect: waits with exponential backoff (2s, 5s, 10s, then 30s indefinitely), then reconnects with the current cursor as `Last-Event-ID`.

### 5.7 Pull cycle (belt-and-suspenders)

Every 60 seconds, each peer runs a pull cycle against every other peer in the group:

1. `GET /memory/since?group_name=X&since=<cursor>` → count of new memories on the remote peer
2. If count > 0: `GET /memory/get?group_name=X&since=<cursor>` → fetch records
3. For each record: same dedup + insert as SSE handler
4. Advance per-peer cursor

This catches drift in three plausible failure modes: (a) SSE bug we haven''t found yet, (b) network corruption silent enough to evade TCP checksums, (c) subscriber cursor accidentally lost (state file corruption). At MVP scale, the pull cycle is cheap and gives us strong eventual-consistency guarantees while SSE is still earning trust.

### 5.8 Failure surfacing

Every failed delivery attempt is communicated to the user / Claude. This is intentional instrumentation for MVP — we don''t yet have empirical data on baseline network reliability. Defaults can be tuned in a future version after observation periods.

Four channels surface failures:

| Channel | When | Carries |
|---|---|---|
| `store_memory` response `delivered_to[]` | Synchronously on store | Per-peer status: `delivered` (SSE push succeeded), `unreachable` (peer offline; pull cycle will catch up) |
| `peer_state.json` | Updated every pull/drain cycle | `consecutive_failures`, `last_failure_at`, `last_failure_reason` per remote peer |
| SessionStart hook | Start of every Claude session | Aggregated peer reachability summary in the `<memfusion_status>` block |
| `<state_dir>/logs/peer-client.log` | Continuous append | Every reconnect, every pull cycle outcome |

### 5.9 Known limitations and the migration path

These limitations are accepted for the MVP and are part of why the mesh model is positioned for small teams:

| Limitation | When it bites | Migration path |
|---|---|---|
| **Mesh fan-out is O(N) per store** | At ≤10 peers: 9 outbound calls per store, trivial. At ≥30 peers: 29 outbound calls per store, becomes painful on slow networks. | Enterprise tier (§14) introduces hub-and-spoke or orchestrator-centric topology that converts O(N) into O(1) per store from the origin''s perspective. |
| **Every peer holds the full group corpus** | At MVP scale (few MB per group): fine. At enterprise scale (GB per group): expensive. | Enterprise tier supports tiered storage and selective replication. |
| **Subscriber connections are O(N²) across the group** | At ≤10 peers: 90 TCP connections in the group total, fine. At ≥50: 2,450 connections. | Enterprise tier with hub topology drops this to O(N). |
| **No authentication / TLS** | LAN-only deployment makes this OK for MVP. | Enterprise tier adds mTLS or token-based auth. |
| **No multi-group membership per peer** | MVP enforces single-group per node. | Enterprise tier supports multi-membership. |
| **Same-microsecond `received_at` collisions** | Single-threaded HTTP daemon serializing PUTs makes this structurally impossible at MVP scale. | If concurrency becomes real, add sequence_number payload field as the cursor instead of received_at. |

---

## 7. Process Architecture

The system uses a **dual-client architecture** over a shared core library:

```
                    ┌──────────────────────────────┐
                    │      core_mem_fusion.py      │
                    │  (memory functions on top    │
                    │   of Qdrant; the actual      │
                    │   logic lives here)          │
                    │                              │
                    │  store_memory()              │
                    │  search_memory()             │
                    │  upsert_memory()             │
                    │  get_new_entries_since(cur)  │
                    │  content_hash(), iso_now()   │
                    └─────────────┬────────────────┘
                                  │
                                  ▼
                              ┌─────────┐
                              │ Qdrant  │
                              │ (single │
                              │  per-   │
                              │  peer   │
                              │  store) │
                              └─────────┘
                                  ▲
                ┌─────────────────┼─────────────────┐
                │                                   │
                ▼                                   ▼
        ┌──────────────────┐               ┌──────────────────┐
        │  mem_fusion.py   │               │ constellation.py │
        │  (Claude proxy)  │               │ (peer-net proxy) │
        │                  │               │                  │
        │  stdio MCP       │               │  HTTP server +   │
        │  per Claude      │               │  SSE pub/sub     │
        │  session         │               │  Always-on       │
        │                  │               │  launchd daemon  │
        │  Calls core      │               │  Calls core      │
        │  functions to    │               │  functions to    │
        │  serve Claude    │               │  serve peers     │
        │  tool requests   │               │  + run mesh      │
        │                  │               │  synchronization │
        └──────────────────┘               └──────────────────┘
```

### 6.1 The shared library: `core_mem_fusion.py`

A pure-Python module that wraps Qdrant for the project''s memory operations. Includes:
- All memory CRUD functions (store, search, upsert, delete, get_related, etc.)
- The cross-client notification primitive: `get_new_entries_since(cursor, source_filter, limit)`
- Shared constants (VECTOR_SIZE=768, EMBED_MODEL="nomic-embed-text", collection name)
- The content_hash function (single source of truth, used by both clients)
- The Ollama embedding helper

Critical invariant: **the same `content_hash` function is used everywhere**. If it ever diverges, dedup breaks across clients.

### 6.2 Client 1: `mem_fusion.py` — Claude proxy

A thin MCP server using stdio transport. Spawned by Claude Code per session. Registers nine MCP tools (`store_memory`, `search_memory`, `search_recent`, `upsert_memory`, `find_or_create`, `delete_memory`, `get_related`, `memory_stats`, `export_record`) and delegates each one to a corresponding `core_mem_fusion` function. Adds no business logic of its own.

Lifecycle: ephemeral. When Claude session ends, the process exits.

### 6.3 Client 2: `constellation.py` — peer-network proxy

An always-on HTTP daemon under launchd management. Runs on every node by default — there is no "orchestrator vs peer" install distinction. Responsibilities:
- Expose HTTP endpoints for peer-to-peer federation (§5)
- Maintain SSE subscriber connections out to every other peer in the group
- Maintain SSE subscriber registry for inbound connections from other peers
- Run the publisher loop (poll core every ~1s for new `source=local` entries; broadcast via SSE)
- Run the pull cycle every 60s (catch any drift)
- Persist per-peer cursor state in `<state_dir>/peer_state.json`

Lifecycle: persistent. Survives Claude sessions; restarts via launchd `KeepAlive`.

### 6.4 No IPC between siblings

The two clients **do not communicate with each other directly**. There is no socket, pipe, signal, or HTTP loopback between them. They communicate exclusively through their shared Qdrant collection.

When mem_fusion writes a memory locally, constellation observes the change on its next poll cycle (~1s) via `core.get_new_entries_since()`. No notification needed; the storage layer is the rendezvous.

This decoupling means:
- Either client can crash, restart, or upgrade without affecting the other
- New clients (a web UI, a Slack bot, an alternate LLM adapter) can be added by importing `core_mem_fusion` — no protocol negotiation with existing clients
- Tests can hit `core_mem_fusion` directly without spinning up either transport

### 6.5 Why not collapse into one daemon

Two reasons the dual-process model is preserved instead of collapsing everything into one always-on HTTP server:

**Lifecycle and transport mismatch.** Claude Code uses stdio MCP — spawn-per-session, no shared state between sessions. Peers use HTTP MCP — always-on, network-bound. Mixing both transports in one process forces compromises in both directions.

**Security isolation.** mem_fusion.py is stdio-only and localhost-only; it cannot be reached from the network at all. constellation.py is HTTP-bound and listens on a configurable interface (default loopback, optionally LAN). Process isolation means even a compromised constellation has only the surface area constellation exposes — it cannot escalate to Claude''s personal-memory surface.

---

## 8. Storage Model

One Qdrant collection per peer, holding all memory the peer knows about (personal and group-shared), distinguished by tags.

### 7.1 Collection schema

```
collection: mem_fusion_memories   (env var MEMFUSION_COLLECTION can override)
  vectors:        768-dim, cosine distance
  payload indexes:
    content_hash      keyword       # dedup
    type              keyword       # decision, fact, preference, error, code, context, session
    project           keyword       # multi-tenant tag (mem-fusion native)
    source            keyword       # local | federation
    origin_node       keyword       # author identity (federation entries only)
    group_name        keyword       # group scope (federation entries only)
    session_id        keyword       # links to Claude session
    importance        integer       # range filters for min_importance
    received_at       datetime      # microsecond ISO; cursor for sync
    timestamp         datetime      # original-author clock (immutable)
```

### 7.2 Entry kinds in the single collection

| Kind | Tags | Sourced by |
|---|---|---|
| Personal memory | `source=local`, no `group_name` or `origin_node` | mem_fusion calls from Claude (store_memory, /remember, hook captures) |
| Group memory received from another peer | `source=federation`, `origin_node=<author>`, `group_name=<scope>` | constellation receiving an SSE event or pull-cycle response |
| (Optional) Personal memory tagged for sharing | `source=local`, `group_name=<scope>` | Personal store that''s also been broadcast to the group (the fan-out source itself stores its own copy as `source=local` first; recipients get `source=federation` copies) |

Search across the collection returns all three kinds together, ranked by semantic similarity. The `source` tag is a payload filter the user can opt into for hygiene (e.g., "only show me the team''s shared knowledge, not my personal notes").

### 7.3 Single source of truth, no replication

Each peer''s Qdrant collection is **its own canonical** for the memories it holds. There is no "replication" between peers — instead, mesh fan-out propagates memories so that every peer''s collection independently converges to the same content via content_hash dedup. Different peers may have memories in slightly different orders or with slightly different `received_at` timestamps (since each peer stamps with its own clock on insert), but the content and original `origin_node` attribution are byte-identical across peers once propagation completes.

This model is what makes the mesh resilient: any single peer''s Qdrant disappearing does not lose the group''s state. As long as at least one peer in the group still has the memory in its local collection, the memory survives and propagates to peers that lost it.

---

## 9. Authentication & Authorization

### v0.3.0 model

- **Swarm key** (shared secret per group, 32 bytes). All members hold their group's swarm key; requests carry it as a Bearer token in the `Authorization` header.
- **Localhost-vs-network tool filtering.** Requests from `127.0.0.1` / `::1` see all `constellation/` tools (this is the local Claude). Requests from any other address are filtered to the same tool set (no privilege difference at v0.3.0; the localhost/network split matters more in mixed-binding scenarios).
- **Per-group authorization.** Every operation requires the caller to present a valid swarm key for the named group AND to be in that group's member list.

### Threat model

What v0.3.0 protects against:
- Random network probes (no swarm key → 401)
- Cross-group requests by valid members of wrong group (member of A trying to read B → 403)
- Direct access to Mem-Fusion's local memory tools from network (impossible by architecture — Mem-Fusion isn't on the network)

What v0.3.0 does NOT protect against:
- A compromised group member acting maliciously against their own group (they hold the swarm key legitimately)
- A misbehaving orchestrating node (single point of authority by design)
- Replay attacks (no nonces yet)
- Rate-based abuse (no per-node rate limits yet)

### Deferred to post-MVP

- mTLS for cross-machine deployments
- Per-node identity (cryptographic keypair instead of shared swarm key)
- Per-tool capability tokens
- Rate limiting and quotas
- Human-review apprenticeship loop for inbound canonical submissions

---

## 10. Lifecycle

### Node startup

1. Read config from `~/.local/share/mem-fusion/src/config.json`.
2. For each `memberships[]` entry: prepare connection info for that group's orchestrating node.
3. For each `orchestrating[]` entry: bind HTTP listener on configured port, initialize this group's canonical store.
4. Begin serving MCP-over-HTTP.

### Joining an existing group

1. Group's human admin generates a swarm key and shares it with the new member out-of-band (Signal, in person, etc.).
2. New member adds to their `memberships` config:
   ```
   {
     "group_name": "engineering@branch",
     "orchestrator_endpoint": "http://10.0.0.5:7433",
     "swarm_key": "<32-byte-hex>"
   }
   ```
3. Restart Constellation daemon (or send SIGHUP).
4. Constellation daemon registers with the orchestrating node by calling `constellation/peers/self` (informational) and beginning to participate.

### Creating a new group

1. Designated orchestrating node generates a swarm key for the group.
2. Adds to its config under `orchestrating[]`:
   ```
   {
     "group_name": "engineering@branch",
     "listen_address": "0.0.0.0:7433",
     "swarm_key": "<32-byte-hex>"
   }
   ```
3. Restart daemon.
4. Distributes swarm key + endpoint to intended members (per the joining flow above).

### Configuration format (v0.3.0)

```json
{
  "node_name": "alice-mac",
  "memberships": [
    {
      "group_name": "engineering@branch",
      "orchestrator_endpoint": "http://10.0.0.5:7433",
      "swarm_key": "..."
    }
  ],
  "orchestrating": []
}
```

For v0.3.0, `memberships` has length 1 and `orchestrating` is either empty (peer node) or has length 1 (the group's orchestrator). Multi-group membership and multi-orchestration are post-MVP.

---

## 11. v0.3.0 Scope Constraints

The following are **explicitly out of scope** for v0.3.0. The protocol is designed to extend cleanly into them later, but they are not implemented in this release.

| Capability | v0.3.0 | Deferred to |
|---|---|---|
| Multi-group membership per node | One group only | post-MVP |
| Hierarchy of orchestrating nodes | Single orchestrator per group, no parent | post-MVP |
| Cross-group memory propagation | N/A (only one group) | post-MVP |
| Dynamic orchestrator election | Statically configured | post-MVP or later |
| Human review / curator / apprenticeship loop | Auto-accept all valid submissions | post-MVP |
| Direct node-to-node communication | Forbidden — always through orchestrating node | Never (architectural invariant) |
| mTLS / per-node identity | Swarm key only | post-MVP |
| Cross-org federation | Single-org / single-constellation | post-MVP |
| Rate limiting / quotas | None | post-MVP |
| libp2p transport | Not used | Likely never (HTTP/HTTPS sufficient) |

### Why v0.3.0 ships small

The minimum architecture that **proves group memory works as a substrate**:
- Multiple Mem-Fusion installs federate via a shared canonical store
- Memory can be submitted by any member and read by any member
- The orchestrating node holds the authority
- The boundary between personal and group memory is real and enforced

Everything else is either (a) deferred because it can be added without breaking v0.3.0, or (b) deferred because we want to learn from v0.3.0 use before designing post-MVP.

---

## 12. Forward Compatibility

The v0.3.0 protocol is designed so that post-MVP extensions don't break existing nodes:

1. **All operations take `group_name`.** In v0.3.0 there's only one group, so this is trivial. In post-MVP the same call shape supports multi-group nodes — pick which group the call targets.

2. **`peers/self` returns `memberships` as a list.** v0.3.0 returns a list of length 1; post-MVP returns multiple entries.

3. **Memory records carry `provenance`.** Empty in v0.3.0 (no cross-group flow exists); populated in post-MVP when memories are promoted between groups.

4. **Config schema supports multiple memberships and orchestrating entries.** v0.3.0 uses length-1 lists; post-MVP extends naturally.

5. **The protocol has no special verb that becomes obsolete.** Cross-group propagation in post-MVP uses the same `memory/put` primitive applied recursively at multi-membership nodes.

No anticipated breaking change at the protocol level between v0.3.0 and v1.0.

---

## 13. Examples

### Example 1: Single-group v0.3.0 — two engineers and a PM share a project context

**Setup:**
- Group: `project-x@branch`, orchestrated by `alice-mac`
- Members: `bob-mac`, `carol-mac`

**Flow:**

1. Alice's Claude session yields an architectural decision worth sharing:
   ```
   alice's Claude → constellation/memory/put(
     group_name: "project-x@branch",
     content: "We decided to use SSE for the streaming endpoint, not WebSockets — reason: simpler retry semantics.",
     type: "decision",
     importance: 4
   )
   ```
2. Alice's machine (the orchestrating node for this group) stores the memory in its canonical store.

3. The next day, Bob's Claude is implementing the streaming endpoint:
   ```
   bob's Claude → constellation/memory/get(
     group_name: "project-x@branch",
     query: "streaming endpoint approach"
   )
   ```
4. Bob's Constellation daemon calls Alice's daemon's `memory/get`. Alice's daemon validates Bob's swarm key, looks up the project-x@branch canonical store, returns matching memories.

5. Bob's Claude receives Alice's decision and proceeds without re-asking Alice.

6. Carol (PM) queries from her angle:
   ```
   carol's Claude → constellation/memory/get(
     group_name: "project-x@branch",
     filters: { type: "decision", since: "7d" }
   )
   ```
7. Carol now has the engineering context that lets her write a more accurate spec.

No data left any of their machines except in service of explicit, authorized requests. Each member sees what's in the group's canonical; no one sees the others' personal memory.

### Example 2: Forward-looking post-MVP — engineering, CTO, product hierarchy

**Setup (post-MVP capability, not v0.3.0):**

- Group `engineering@branch`: orchestrated by `eng-mgr-mac`, members `[alice-mac, bob-mac, carol-mac]`
- Group `leadership@branch`: orchestrated by `cto-mac`, members `[eng-mgr-mac, product-mgr-mac, design-mgr-mac]`
- Group `product@branch`: orchestrated by `product-mgr-mac`, members `[dave-mac, erin-mac]`

`eng-mgr-mac` plays orchestrating role in engineering, and *non-orchestrating member* role in leadership. Same node, two roles.

**Flow — engineering memory rises to leadership visibility:**

1. Alice submits a memory to engineering:
   ```
   alice → constellation/memory/put(group_name: "engineering@branch", content: "API rate limits ...")
   ```
2. Stored in engineering's canonical. End of automatic propagation.

3. `eng-mgr-mac` (the orchestrating node of engineering, also a member of leadership) reviews and decides this memory belongs in leadership's canonical. Explicit second call:
   ```
   eng-mgr → constellation/memory/put(
     group_name: "leadership@branch",
     content: "API rate limits ...",
     provenance: { origin_group: "engineering@branch", promoted_by: "eng-mgr-mac", promoted_at: "2026-..." }
   )
   ```
4. `cto-mac` (orchestrating leadership) validates membership of `eng-mgr-mac`, accepts the memory into leadership's canonical with provenance preserved.

5. Now `product-mgr-mac` (member of leadership) can query leadership and see this memory. They can choose to further promote it into product's canonical via the same pattern if it's relevant for their team.

The hierarchy is not a special protocol feature. It's just nodes with overlapping memberships doing direct memory/put calls. The cost is one explicit call per cross-group propagation; no automatic upward flow.

### Example 3: A node belonging to multiple groups (post-MVP)

Alice is on engineering AND on a cross-functional task force.

```
alice's config (post-MVP):
{
  "node_name": "alice-mac",
  "memberships": [
    { "group_name": "engineering@branch", "orchestrator_endpoint": "..." },
    { "group_name": "task-force-q3@branch", "orchestrator_endpoint": "..." }
  ],
  "orchestrating": []
}
```

Alice's Claude can put memories into either group:
- `memory/put(group_name: "engineering@branch", ...)` — visible to engineering members
- `memory/put(group_name: "task-force-q3@branch", ...)` — visible to task force members
- The two are disjoint canonicals; cross-pollination requires explicit dual-put

When the task force disbands, Alice removes the membership from her config. Her engineering membership is unaffected.

---

## 14. Enterprise tier — at-scale design (post-MVP)

The MVP is positioned for **small teams: ≤10 peers per group, single local network**. Multiple architectural decisions trade away scale to keep the MVP simple, debuggable, and self-sufficient. The following capabilities are deliberately deferred and represent a future **commercial enterprise tier** for organizations operating at scale.

### 13.1 What MVP gives up to stay simple

| MVP choice | Scale ceiling | Enterprise version |
|---|---|---|
| Mesh fan-out (every store → N-1 outbound calls) | ≤10 peers per group; N=30 starts to feel slow | Hub-and-spoke topology: orchestrating node(s) hold the canonical; peers push once to the hub, pull from the hub. Per-store cost from origin is O(1). |
| Every peer holds the full group corpus | Few-MB scale | Tiered storage: hot tier on peers, cold tier on shared/managed storage. Selective replication based on access patterns. |
| O(N²) SSE connections within the group | ≤10 peers (≤90 connections) | Centralized broker (NATS, cloud pub/sub, or managed broker) reduces per-peer connections to O(1) at the cost of an external dependency. |
| Local-network-only deployment, no auth | LAN scope | mTLS, IAM-backed auth, audit logging, multi-tenant isolation. Cloud-hosted brokers handle WAN traversal and NAT. |
| Single-group membership per peer | One project / one team | Multi-group membership with cross-group routing rules and per-group policies. |
| No conflict resolution beyond content_hash dedup | Single-threaded peers; rare concurrent same-content writes | Vector clocks or sequence numbers; explicit conflict resolution for `upsert_memory` style mutations. |
| `received_at` cursor (microsecond ISO timestamps) | Single-orchestrator concurrency model | Per-group monotonic `sequence_number` assigned by the canonical; survives multi-threaded orchestrator implementations. |
| No replication / no canonical copy beyond peers' own stores | Group is "alive" only as long as ≥1 peer is online | Managed durable storage tier; group state persists independent of peer availability. |

### 13.2 Why deferring this is the right call

Each enterprise capability above has a real implementation cost, a real ongoing operational cost (broker management, IAM setup, storage tiering), and a real complexity cost in the protocol and code. For ≤10 person teams on a LAN, those costs aren't repaid by value. Building them into the MVP would slow shipping, complicate the install story, and lock in design decisions before we've learned anything from real usage.

The MVP exists to **validate the core product hypothesis**: that small teams want shared AI memory and will use it if it's frictionless to install and intuitive to operate. Until that's validated, scaling work is premature optimization.

### 13.3 The commercial wedge

The enterprise tier is the natural product split:

- **MVP / Open source**: small teams, LAN deployments, self-contained install, no external services, no recurring cost.
- **Enterprise**: managed deployment, cross-WAN federation, mTLS auth, audit logs, multi-tenant isolation, IAM-backed access control, per-group policy enforcement, durable group state independent of peer uptime. Paid offering.

This split is what makes the MVP architectural simplifications acceptable. We're not building "the cheap version of the real product." We're building the right product for small teams, and reserving the operational complexity for the population that needs it and can pay for it.

### 13.4 Migration path

Each MVP design decision maps to a non-disruptive enterprise upgrade path:

- **Mesh → hub-and-spoke**: peers' fan-out targets become a single orchestrator URL instead of a peer list. No protocol changes; only configuration.
- **`received_at` cursor → `sequence_number` cursor**: the wire protocol stays cursor-based; the cursor format becomes opaque (peers don't interpret it; just pass it back).
- **No auth → mTLS**: HTTP endpoints add TLS termination; client certificates carry node identity. Wire protocol shapes don't change.
- **Single group → multi-group**: configuration adds `memberships[]` entries; runtime adds membership lookup; protocol stays group-scoped.

The MVP isn't a throwaway. It's the foundation; the enterprise tier adds operational features on top without rewriting the core.

---

## 15. Open Questions

The following are known unresolved decisions. They don't block v0.3.0 implementation but should be settled before v0.3.0 ships:

1. **Storage layout: single collection vs. per-group collection?** Single is simpler; per-group is cleaner for isolation and lifecycle. Implementation will choose; either preserves the protocol.

2. **Swarm key rotation.** v0.3.0 has no key rotation mechanism. If a swarm key is compromised, the group's only recourse is to generate a new one and redistribute. post-MVP should add a key-rotation primitive.

3. **Snapshot semantics for `memory/get` with no filters.** Returns ALL memories? Paginated? Capped at N? Decide based on expected canonical store size (likely capped at 1000 with a `cursor` pagination field).

4. **What happens when a member's swarm key is removed from the orchestrator's allow-list mid-session?** Should in-flight requests complete or fail? Likely: in-flight completes; new requests fail. Worth confirming.

5. **Should Constellation's persistent daemon survive Mem-Fusion uninstall?** Currently they're independent; uninstalling Mem-Fusion leaves Constellation running. May be desirable (gateway deployments) or surprising (user expected full cleanup). Decide UX.

6. **Cross-platform launchd-equivalent.** Constellation depends on launchd (macOS) for daemon management. Linux/Windows support requires systemd / Windows Services equivalents. Out of v0.3.0 scope but worth scoping later.

---

## 16. What Constellation is NOT

Closing with a clear list of things Constellation deliberately is not, so future contributors don't try to bend it into them:

- **Not a peer-to-peer mesh.** All relationships flow through orchestrating nodes.
- **Not a generic message bus.** The protocol is scoped to memory federation; Constellation is not Kafka or NATS.
- **Not a workflow engine.** Constellation moves memory between groups; it does not run scheduled jobs or trigger external integrations.
- **Not a cloud service.** Constellation runs locally on each member's machine. No SaaS, no cloud component.
- **Not Mem-Fusion.** Mem-Fusion is the substrate for personal memory; Constellation is the substrate for group memory. Same repo, distinct daemons, complementary purposes.

---

*Draft architecture document · 2026-05-10 · awaiting review and ratification*
