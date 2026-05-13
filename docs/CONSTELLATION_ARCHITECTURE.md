# Constellation — Architecture

**Status:** Current as of 2026-05-12. Reflects what v0.3.0 ships.

Constellation lets Mem-Fusion peers share memory with each other. Where Mem-Fusion keeps memory on one machine, Constellation sends new memories to every other peer in the same group so they all see the same thing. It runs as a sibling daemon to Mem-Fusion: same machine, same Qdrant collection, separate HTTP MCP surface.

---

## 1. Mission

Make AI memory non-ephemeral across the boundary that matters most — between machines (the same person's laptop and desktop) or between teammates (two engineers on the same project). One Qdrant collection per peer. Memory written on any peer appears on every other peer in the same group, byte-identically, without re-embedding.

Mem-Fusion answers "remember across sessions." Constellation answers "remember across machines."

---

## 2. Primary use cases

### 2.1 Primary: one user, multiple machines

The dominant case. A single person uses Claude Code on a laptop and a desktop (or work and home). They want every memory they've stored — every decision, preference, error fix — available regardless of which machine the session is open on. There is no trust boundary between the machines; they're all the user's.

### 2.2 Secondary: small team, shared knowledge

Several engineers on the same project. A decision one of them makes ("we use gRPC for internal RPC") should be available to a teammate's Claude when relevant. Trust is bounded by group membership; out-of-band agreement gates joining.

### 2.3 What's the same across both cases

Same code path. Same protocol. Same storage. The only difference is who the peers are. MVP group size: ≤10 peers.

---

## 3. Object model

### Node (or peer)

A participant in a group. One Mem-Fusion install on one machine = one node. Each node has:

- a local Qdrant (the `cowork_memories` collection — shared with Mem-Fusion)
- a Constellation HTTP daemon listening on `:7433` (default)
- a config listing the groups it belongs to

Every node is **symmetric**. There is no orchestrator, no privileged peer, no central node. Every node holds its own local store and both publishes its own memories and receives memories from others.

### Group

A named set of peers that have agreed to share memory. Identified by `group_name` (e.g. `"engineering@branch"`). Groups are flat — no hierarchy. Membership is set in each peer's config (no automatic discovery in MVP).

The members of a group form a small peer network. Every peer in the group knows every other peer in the group. The group's collective state is the union of every peer's local store.

---

## 4. Routing rules

Four rules. Sharing behavior is fully defined by them.

1. **Send to every peer, no relays.** When a node writes a memory locally, it sends a copy directly to every other peer in the group. No intermediate hops, no proxies, no relay nodes.

2. **Receivers do NOT re-send.** A peer that *receives* a `POST /memory/put` does not pass it onward. Doing so would multiply each write into N² messages. Receivers only insert into local Qdrant tagged `source=group`.

3. **Membership equals authorization.** Any peer in a group can publish to or read from that group. No per-peer ACLs.

4. **No automatic cross-group propagation.** A node that belongs to two groups does NOT bridge them. Memory is scoped to the group it was written into.

### Why send-to-every-peer, not hub-and-spoke

MVP group size (≤10 peers) makes per-write fan-out trivial — 9 outbound HTTP calls. Routing everything through a single hub peer buys nothing here and adds a single point of failure. Hub-and-spoke is the right shape only at enterprise scale (>30 peers); deferred to post-MVP.

---

## 5. Protocol surface

All endpoints scoped by `group_name`. Every peer exposes every endpoint. Localhost binding by default.

| Endpoint | Purpose | Status |
|---|---|---|
| `POST /memory/put` | Receive a memory from another peer | ✅ Implemented |
| `GET /memory/get?group_name=&id=` | Fetch a shared memory by ID | ✅ Implemented |
| `GET /memory/get?group_name=&limit=` | Scroll recent shared memories | ✅ Implemented |
| `GET /peers?group_name=` | Directory of peers known to this node (inferred from PUT activity) | ✅ Implemented |
| `GET /peers/self` | This node's identity + memberships | ✅ Implemented |
| `GET /health` | Liveness probe | ✅ Implemented |
| `GET /memory/since?group_name=&cursor=` | Incremental catch-up fetch | 🚧 Planned (next milestone) |
| `GET /memory/events` (SSE) | Long-lived stream of new entries | 🚧 Planned (next milestone) |

### `POST /memory/put`

Validate `content_hash` (recompute and compare — reject on mismatch). Validate vector dimension. Dedup on `(content_hash, group_name, source=group)`. Insert with full provenance payload (`origin_node`, `group_name`, `submitted_at`, `received_at`, `submission_kind`).

### `GET /memory/get`

Two modes. By-ID returns one shared memory, vector and all. Scroll mode returns recent shared memories in the group (with a configurable limit).

### `GET /peers`

Directory inferred from each shared memory's `origin_node` payload field. A peer that has never submitted a memory to this node is not listed — this is fine for MVP (visibility follows interaction).

### What is NOT in the protocol

- **No group-wide search.** Each peer searches its own collection. The group network is for sharing writes, not for routing queries between peers.
- **No memory editing across the group.** Shared entries are append-only on each receiving peer.
- **No automatic peer discovery.** Memberships are set in each peer's config.

---

## 6. Storage model

Constellation does not own a separate Qdrant collection. It writes into the same `cowork_memories` collection that Mem-Fusion uses, distinguishing entries by a `source` payload tag:

| Tag | Origin | Carries |
|---|---|---|
| `source=local` | Written by Mem-Fusion on this peer | `content`, `vector`, `content_hash`, `timestamp`, `type`, `tags`, `project`, `importance` |
| `source=group` | Received via `POST /memory/put` from another peer | Same fields + `origin_node`, `group_name`, `received_at`, `submitted_at`, `submission_kind` |

`timestamp` on shared entries is aliased to `received_at` so Mem-Fusion's time-filtered queries (`search_recent`, etc.) surface them naturally — same query, same shape, same vector format — without code paths having to know the difference.

**Dedup is on `(content_hash, group_name, source=group)`.** A peer's local copy and a copy received from another peer with the same content can coexist; they're different events ("I wrote this" vs. "I received this from X").

**No replication state.** Each peer's Qdrant is the source of truth for what that peer knows. No replication ledger, no conflict resolution, no consensus protocol. Convergence is eventual via per-write fan-out.

---

## 7. Synchronization

### Currently (v0.3.0): send on write

When a node writes a memory locally:

1. Mem-Fusion stores it (`source=local`) in this peer's Qdrant via `core.store_memory`.
2. CLAUDE.md instructs Claude to call `mem-fusion/export_record(id=<local_id>)` to extract the full record (incl. 768-dim vector).
3. Claude calls `constellation/memory/put(...)` on each remote peer in the group.

Send-on-write only. This guarantees forward-going writes reach peers that are online but doesn't backfill peers offline at write time. The publisher sees the failure for any peer it couldn't reach — no silent queueing.

### Planned: SSE pub/sub + pull safety net

Two transports together cover both online and offline-at-write-time cases:

- **SSE primary.** Each peer subscribes via long-lived `GET /memory/events?group_name=` connections to every other peer. New writes stream in real-time. Each subscriber tracks its own cursor (`received_at` timestamp).
- **Pull safety net.** Every 60 seconds each peer calls `GET /memory/since?cursor=` on every other peer to catch what SSE missed.

Both share the same primitive: `core.get_new_entries_since(cursor, source_filter, limit)` — already extracted into `core.py` for this purpose.

---

## 8. Process architecture

```
   ┌─────────────────────┐              ┌─────────────────────────┐
   │   mem_fusion.py     │              │    constellation.py     │
   │  (stdio MCP)        │              │   (HTTP MCP, :7433)     │
   │  served to Claude   │              │   served to peers       │
   └─────────────────┬───┘              └───┬─────────────────────┘
                     │                       │
                     └──────┐       ┌────────┘
                            ▼       ▼
                       ┌─────────────┐
                       │   core.py   │
                       │   (shared)  │
                       └──────┬──────┘
                              ▼
                       ┌─────────────┐
                       │   Qdrant    │
                       │  cowork_    │
                       │  memories   │
                       └─────────────┘
```

### `core.py` — the shared library

The only module that talks to Qdrant. Contains the Qdrant client, the Ollama embedding helper, all 9 memory operations (`store_memory`, `search_memory`, `export_record`, etc.), and the `get_new_entries_since` notification helper. Both daemons import it.

### `mem_fusion.py` — Claude's MCP proxy (stdio)

A ~130-line wrapper that translates MCP JSON-RPC over stdio into calls to `core.py`. One subprocess per Claude session. Localhost-only by design — no network listener.

### `constellation.py` — group peer's MCP proxy (HTTP)

A persistent HTTP daemon under launchd. Exposes the protocol surface from §5 to other peers in the group. Imports `core.py` for storage; never talks directly to Mem-Fusion.

### Why two daemons, not one

The HTTP listener is a public attack surface. Mem-Fusion's stdio serves only the Claude subprocess it's parented to. Process isolation means a compromised Constellation cannot read or modify Mem-Fusion's in-memory state directly. The daemons communicate only through Qdrant.

### No IPC

Qdrant is the meeting point. Either daemon writes to `cowork_memories`; either daemon reads from `cowork_memories`. The `source` tag tells them which entries are "theirs." A freshly-installed Constellation picks up Mem-Fusion's existing memory with no handshake.

---

## 9. Configuration

```json
{
  "node_name": "alice-mac",
  "listen_address": "127.0.0.1:7433",
  "qdrant_url": "http://127.0.0.1:6333",
  "state_dir": "~/.local/share/mem-fusion/constellation",
  "memberships": [
    { "group_name": "engineering@branch" }
  ]
}
```

`memberships` lists group names this peer belongs to. There is no role per membership — every member is a full participant in the group. v0.3.0 restricts `memberships` to length 1; multi-group membership is post-MVP.

---

## 10. Lifecycle

### Node startup

1. Read config.
2. Init the shared Qdrant collection (idempotent — no-op if Mem-Fusion already initialized it).
3. Bind HTTP listener on `listen_address`.
4. Begin serving the protocol surface for each group in `memberships`.

### Joining a group

1. New peer adds the `group_name` to its `memberships`.
2. Restart Constellation daemon.
3. New peer is in the group. Existing peers learn of it the first time it publishes (or, once `/memory/since` ships, on the next pull cycle).

### Creating a new group

There is no creation ritual. The first peer to configure a group with a given `group_name` *is* the group. Subsequent peers join by adding the same name to their config.

---

## 11. Authentication (v0.3.0)

None. Both daemons bind to `127.0.0.1` by default. Peers reaching each other across machines do so over a private network (LAN, Tailscale, VPN) — Constellation does not authenticate the connection.

Intentional for MVP. The threat model is "machines I own and people I work directly with." Swarm-key Bearer auth, mTLS, and per-peer Ed25519 identities are designed for post-MVP but not implemented.

---

## 12. v0.3.0 scope

### Implemented

- Symmetric peers (no roles)
- Single Qdrant collection per peer with source tagging
- Send-on-write group sharing
- Single-group membership per node
- `content_hash` dedup + verbatim-vector integrity invariants
- Localhost-only listener; no auth

### Deferred to post-MVP

- SSE pub/sub and pull safety net (`/memory/since`, `/memory/events`)
- Multi-group membership per node
- Auth (swarm key, mTLS, per-peer identity)
- Human review / curator / apprenticeship loop (MVP ships auto-share)
- Cross-group propagation
- Automatic peer discovery
- Enterprise tier (hub-and-spoke, >30-peer scale)

---

## 13. What Constellation is NOT

- **Not a distributed database.** It's a write-sharing layer over independent Qdrant stores. No consensus, no replication ledger, no conflict resolution.
- **Not a queue.** No persistent queues. If a peer is offline at write time, the publisher sees the failure.
- **Not group-wide search.** Each peer searches its own Qdrant. The group network is for sharing writes only.
- **Not multi-tenant.** One peer = one user. Multi-user-per-peer is not on the roadmap.
- **Not cross-org.** Memberships are within one Constellation install. Cross-org sharing is post-MVP.

---

*See [`MEM_FUSION_ARCHITECTURE.md`](MEM_FUSION_ARCHITECTURE.md) for the single-node design that Constellation extends.*
