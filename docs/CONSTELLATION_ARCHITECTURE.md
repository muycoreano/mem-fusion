# Constellation — Architecture

> **⚠ Superseded** — The wire format and routing described below (singular `group_name` payload, `source=local`/`source=group` tags, single-membership-per-node, push augments the local entry in place) were replaced in v0.4 by a `groups: list[str]` payload, multi-membership configs, push-time group filtering, additive dedup-merge, and a new `group_push(group, memory_ids?)` signature. v0.5 then positions Constellation as the **optional P2P sibling system** alongside the new connector model in mem-fusion native. For the current architecture see [`v0.5_CONNECTOR_ARCHITECTURE.md`](v0.5_CONNECTOR_ARCHITECTURE.md) (connectors), [`v0.4_MEMORY_ARCHITECTURE.md`](v0.4_MEMORY_ARCHITECTURE.md) §6–§7 (group-keyed wire protocol still operational during transition), and [`../constellation/README.md`](../constellation/README.md) (Constellation's role in v0.5). This document remains as a historical record of the v0.3 design path.

**Status:** v0.3.0 architecture (historical); v0.5 positions Constellation as the optional P2P sibling system.

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

Constellation exposes two HTTP surfaces in one process, on two ports:

- **Peer surface** (bound to `0.0.0.0:7533`) — what other peers' Constellation daemons call. Network-reachable on LAN/VPN.
- **Gateway surface** (bound to `127.0.0.1:7534`) — what the local Mem-Fusion process calls. Localhost-only by network binding.

### Peer surface (peer ↔ peer)

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness probe; returns node identity + memberships |
| `GET /peers/self` | This node's identity + memberships + listen address |
| `GET /peers?group_name=` | Directory of peers known to this node, inferred from received-memory `origin_node` activity |
| `POST /memory/put` | Receive a memory from another peer |
| `GET /memory/get?group_name=&id=` | Fetch a group entry by ID |
| `GET /memory/get?group_name=&limit=` | Scroll recent group entries |
| `GET /memory/since?group_name=&cursor=` | Pull entries with `submitted_at > cursor` (called during another peer's pull) |

### Gateway surface (mem-fusion → constellation, localhost-only)

| Endpoint | Purpose |
|---|---|
| `POST /pull` | Pull new entries from every peer in the group; insert deduped; return per-peer telemetry |
| `POST /push` | Augment a local entry with group metadata, then send to every peer in the group; return per-peer delivery telemetry |

### `POST /memory/put` (peer surface)

Receives a flat record from another peer:
```json
{
  "content": "...", "content_hash": "...", "vector": [...],
  "type": "...", "tags": [...], "project": "...", "importance": 4,
  "group_name": "...", "origin_node": "...", "submitted_at": "..."
}
```
Validates `content_hash` (recompute and compare — reject on mismatch). Validates vector dimension. Dedups on `(content_hash, group_name)` — source-agnostic. Inserts tagged `source=group` with `received_at = now()` and `timestamp = received_at`. Returns `{status: "stored"|"duplicate", id}`.

### `GET /memory/since` (peer surface)

Returns entries where `(group_name == X) AND (submitted_at > cursor)`, sorted ascending by `submitted_at`. Full records including vector — no re-embedding required at the receiver. The cursor key is `submitted_at` (origin's timestamp) so it's comparable across peers regardless of who's answering.

### `POST /pull` (gateway surface)

Mem-fusion calls this to refresh the local store from group peers. Daemon:
1. Reads `memberships[].peers[]` from config.
2. Derives cursor as `max(submitted_at)` over local Qdrant entries in the group (no stored cursor state).
3. Calls `GET /memory/since?group_name=&cursor=` on each peer.
4. Inserts returned entries into local Qdrant tagged `source=group`, deduped by `(content_hash, group_name)`.
5. Returns per-peer telemetry. See §7 for the response shape.

### `POST /push` (gateway surface)

Mem-fusion calls this after `store_memory` to share a memory with the group. Body: `{record: {id, content, content_hash, vector, ...}}`. Daemon:
1. Augments the local entry (by ID) with `group_name`, `origin_node` (= self), `submitted_at` (= now).
2. Sends the same record + group metadata to every peer in the group via `POST /memory/put`.
3. Returns per-peer delivery telemetry.

### What is NOT in the protocol

- **No group-wide search.** Each peer searches its own collection.
- **No memory editing across the group.** Group entries are append-only on each receiving peer.
- **No automatic peer discovery.** `memberships[].peers[]` is statically configured.
- **No background sync.** All sharing is user-initiated through `/remember` or `/remember pull`.

---

## 6. Storage model

Constellation does not own a separate Qdrant collection. It writes into the same `cowork_memories` collection that Mem-Fusion uses, distinguishing entries by a `source` payload tag:

| Tag | Origin | Carries |
|---|---|---|
| `source=local` | Written by Mem-Fusion on this peer | `content`, `vector`, `content_hash`, `timestamp`, `type`, `tags`, `project`, `importance` |
| `source=group` | Received from another peer via `POST /memory/put` or `POST /pull` | Same fields + `group_name`, `origin_node`, `submitted_at`, `received_at` |

When a peer **pushes** a local memory to its group, the originator's `source=local` entry is augmented in place: `group_name`, `origin_node` (= self), `submitted_at` (= push time) are added to its payload. The entry stays `source=local` (it's still locally-originated) but now carries group identity so it shows up in subsequent pull queries from other peers.

`timestamp` on group entries (whether origin-side or receive-side) reflects local store/receive time, so Mem-Fusion's time-filtered queries (`search_recent`, etc.) surface group entries naturally — same query, same shape, same vector format — without code paths having to know the difference.

**Dedup is on `(content_hash, group_name)` — source-agnostic.** This is what prevents an originator from receiving back its own pushed memory as a separate `source=group` entry during a subsequent pull. Once any peer has content X for group G, further inserts of the same `(content_hash, group_name)` are dropped as duplicates.

**No replication state.** Each peer's Qdrant is the source of truth for what that peer knows. No replication ledger, no conflict resolution, no consensus protocol. Convergence is eventual via push-on-write plus user-initiated pulls.

---

## 7. Synchronization

All sharing is **user-initiated** in v0.3.0. There is no background timer in Constellation, no SSE pub/sub, no automatic catch-up cycle. The user decides when memory moves between peers; Claude executes the decision via two MCP tools on Mem-Fusion.

### Push (write distribution) — automatic on `/remember`

CLAUDE.md instructs Claude that after `store_memory` returns successfully, also call `mem-fusion/group_push(id=<local_id>)`. The flow:

1. Mem-Fusion stores the memory locally (`source=local`).
2. Mem-Fusion's `group_push` tool fetches the full record via `core.export_record(id)` and POSTs it to the local Constellation gateway at `POST 127.0.0.1:7534/push`.
3. The gateway augments the local entry in place with `group_name`, `origin_node` (= self), `submitted_at` (= now).
4. The gateway fans out `POST /memory/put` to every peer in the group's `peers[]` list.
5. The gateway returns per-peer delivery telemetry to Mem-Fusion.
6. Claude renders a per-peer summary to the user.

If a peer is unreachable at push time, that peer's entry in the telemetry says `status: "unreachable"` with a `reason`. The publisher knows what didn't get through. The offline peer will pick up the memory when *anyone* in the group later issues a `/remember pull`.

### Pull (catch-up) — manual via `/remember pull`

The user invokes `/remember pull`. Claude calls `mem-fusion/group_pull({})`. The flow:

1. Mem-Fusion's `group_pull` tool POSTs to the local Constellation gateway at `POST 127.0.0.1:7534/pull`.
2. The gateway derives the cursor as `max(submitted_at)` over local group entries (no stored state).
3. The gateway calls `GET /memory/since?group_name=&cursor=` on every peer in `peers[]`.
4. Each peer returns entries with `submitted_at > cursor`, vector and all, sorted ascending.
5. The gateway inserts each new entry locally tagged `source=group`, deduped by `(content_hash, group_name)`.
6. The gateway returns per-peer telemetry with `entry_ids` of newly-inserted entries (or `reason` for unreachable peers).
7. Claude renders a per-peer summary to the user; for entries the user asks about, Claude fetches content via `export_record(id)` or `search_recent`.

### Telemetry response shapes

Both `POST /pull` and `POST /push` return per-peer telemetry — never raw content. The data lives in Qdrant; the telemetry tells Claude what happened.

`POST /pull` response:
```json
{
  "peers": [
    {"node_name": "alice", "group_name": "g", "status": "responsive", "entry_ids": [...]},
    {"node_name": "carol", "group_name": "g", "status": "unreachable", "reason": "..."}
  ]
}
```

`POST /push` response:
```json
{
  "peers": [
    {"node_name": "alice", "group_name": "g", "status": "responsive", "delivery": "stored"},
    {"node_name": "bob",   "group_name": "g", "status": "responsive", "delivery": "duplicate"},
    {"node_name": "carol", "group_name": "g", "status": "unreachable", "reason": "..."}
  ]
}
```

Claude derives counts and aggregates by walking the array; the daemon doesn't pre-compute them.

### Why user-initiated, not automatic

The user controls when their group memory is "freshened." No silent background traffic. No clock-driven daemon decisions. The complexity of automatic sync (SSE reconnects, cursor-per-subscription state, pull-cycle rotation) buys little at MVP scale and obscures what the system is doing. If real usage shows manual pulls are too friction-heavy, automatic sync can be added later — Claude can invoke `group_pull` from a hook or installed prompt, keeping the orchestration on the AI side rather than the daemon side.

---

## 8. Process architecture

```
        Claude
          │  MCP stdio (the only MCP surface Claude talks to)
          ▼
   ┌────────────────────────────────┐
   │   mem_fusion.py                │
   │   (stdio MCP server)           │
   │                                │
   │   11 tools:                    │
   │   • 9 memory tools → core.py   │
   │   • group_pull  → gateway      │
   │   • group_push  → gateway      │
   └─────┬──────────────────────┬───┘
         │                      │
         │ core.py              │ HTTP to 127.0.0.1:7534
         │ (Qdrant access)      │ (group_pull / group_push)
         ▼                      ▼
   ┌─────────────┐    ┌──────────────────────────────────┐
   │   Qdrant    │    │  constellation.py (one process)  │
   │  cowork_    │    │                                  │
   │  memories   │    │  ┌────────────────────────────┐  │
   └─────────────┘    │  │ Peer surface 0.0.0.0:7533  │  │
         ▲           │  │ (LAN-reachable)            │◄─┼── remote peers
         │           │  │ /health /peers/self /peers │  │
         │ core.py   │  │ /memory/put /memory/get    │  │
         │           │  │ /memory/since              │  │
         │           │  └────────────────────────────┘  │
         │           │                                  │
         │           │  ┌────────────────────────────┐  │
         │           │  │ Gateway     127.0.0.1:7534 │  │
         └───────────┼──┤ (localhost-only)           │  │
                     │  │ /pull   /push              │  │
                     │  └─────────┬──────────────────┘  │
                     │            │ outbound HTTP       │
                     │            ▼ fan-out             │
                     │   remote peers' :7533 listeners  │
                     └──────────────────────────────────┘
```

### `core.py` — the shared library

The only module that talks to Qdrant. Contains the Qdrant client, the Ollama embedding helper, all 9 memory operations (`store_memory`, `search_memory`, `export_record`, etc.), plus group-pull helpers (`get_entries_for_pull`, `max_submitted_at_in_group`). Both daemons import it.

### `mem_fusion.py` — the MCP server (stdio)

A thin wrapper that translates MCP JSON-RPC over stdio into calls to `core.py` (for local memory ops) or to the local Constellation gateway over HTTP (for `group_pull` and `group_push`). One subprocess per Claude session. Localhost-only by design — no network listener.

### `constellation.py` — group backend (HTTP, two listeners)

A persistent HTTP daemon under launchd. **Two listeners** in one process:

- **Peer surface** (`0.0.0.0:7533`) — what other peers' Constellation daemons call. Network-reachable on LAN/VPN. Receives pushed memories (`POST /memory/put`) and answers pull queries (`GET /memory/since`).
- **Gateway surface** (`127.0.0.1:7534`) — what the local Mem-Fusion process calls. Localhost-only by network binding. Drives outbound peer fan-out for `POST /pull` and `POST /push`.

Constellation is **not** an MCP server. Claude only talks to one MCP server — `mem_fusion.py`. Constellation is a backend that Mem-Fusion delegates group operations to.

### Why two daemons, not one

The peer listener is a public attack surface (LAN-reachable). Mem-Fusion's stdio MCP serves only the Claude subprocess it's parented to. Process isolation means a compromised Constellation cannot read or modify Mem-Fusion's in-memory state directly. The daemons communicate only through:
- Qdrant (shared collection, source-tagged entries)
- The local gateway HTTP surface (Mem-Fusion → Constellation only)

### Why two listeners on one daemon

The peer surface and gateway surface have different threat models — peer surface is exposed to other machines; gateway is local-only. Binding them to different addresses gives **network-enforced separation**: traffic from a remote peer cannot reach the gateway port regardless of any path or auth logic in the handler.

### Graceful degradation

If Constellation isn't installed/running, `group_pull` and `group_push` on Mem-Fusion return `{error: "constellation_not_installed"}`. Mem-Fusion still works for all local-memory operations. Group memory is a strict opt-in — installing Constellation activates the group tools without re-registering anything with Claude.

### No IPC between daemons

Qdrant is the rendezvous. Mem-Fusion writes to `cowork_memories`; Constellation writes to `cowork_memories`. The `source` tag tells them which entries are "theirs." A freshly-installed Constellation picks up Mem-Fusion's existing memory with no handshake.

---

## 9. Configuration

```json
{
  "node_name": "alice-mac",
  "peer_listen_address":    "0.0.0.0:7533",
  "gateway_listen_address": "127.0.0.1:7534",
  "qdrant_url": "http://127.0.0.1:6333",
  "state_dir":  "~/.local/share/mem-fusion/constellation",
  "memberships": [
    {
      "group_name": "engineering@branch",
      "peers": [
        { "node_name": "bob-mac",       "endpoint": "http://10.0.0.5:7533" },
        { "node_name": "carol-laptop",  "endpoint": "http://10.0.0.7:7533" }
      ]
    }
  ]
}
```

| Field | Purpose |
|---|---|
| `node_name` | This peer's identifier (used as `origin_node` on every memory it shares) |
| `peer_listen_address` | Address for the peer surface. `0.0.0.0:7533` exposes on LAN for other peers' Constellation daemons to reach |
| `gateway_listen_address` | Address for the Mem-Fusion gateway. `127.0.0.1:7534` binds localhost-only |
| `qdrant_url` | The local Qdrant instance Mem-Fusion and Constellation both use |
| `memberships[].group_name` | Which group this peer belongs to (v0.3.0 = exactly one) |
| `memberships[].peers[]` | Other peers in the group — each entry has `node_name` and `endpoint` (the peer's `http://host:7533` URL) |

There is no role per membership — every peer is a full participant. v0.3.0 restricts `memberships` to length 1; multi-group membership is post-MVP. Peers are statically configured; no automatic discovery.

---

## 10. Lifecycle

### Node startup

1. Read config.
2. Init the shared Qdrant collection (idempotent — no-op if Mem-Fusion already initialized it).
3. Bind two HTTP listeners — peer (`peer_listen_address`) and gateway (`gateway_listen_address`).
4. Serve both surfaces concurrently in separate threads. Begin accepting peer pushes and gateway calls from Mem-Fusion.

### Joining a group

1. New peer adds the `group_name` to its `memberships`.
2. Restart Constellation daemon.
3. New peer is in the group. Existing peers learn of it the first time it publishes (or, once `/memory/since` ships, on the next pull cycle).

### Creating a new group

There is no creation ritual. The first peer to configure a group with a given `group_name` *is* the group. Subsequent peers join by adding the same name to their config.

---

## 11. Authentication (v0.3.0)

None. The peer surface binds to `0.0.0.0:7533` for LAN reachability; the gateway surface binds to `127.0.0.1:7534`. Peers reaching each other across machines do so over a private network (LAN, Tailscale, VPN) — Constellation does not authenticate the connection.

Intentional for MVP. The threat model is "machines I own and people I work directly with." Swarm-key Bearer auth, mTLS, and per-peer Ed25519 identities are designed for post-MVP but not implemented.

---

## 12. v0.3.0 scope

### Implemented

- Symmetric peers (no roles)
- Two-listener architecture (peer surface + localhost-only gateway surface)
- Single Qdrant collection per peer with `source` tagging
- Constellation as Mem-Fusion's group backend (not a separate MCP server)
- `group_pull` and `group_push` MCP tools on Mem-Fusion
- Push-on-`/remember` (automatic) + manual pull via `/remember pull`
- Single-group membership per node
- Static peer config (no discovery)
- `content_hash` dedup + verbatim-vector integrity invariants

### Deferred to post-MVP

- Automatic background sync (SSE pub/sub or timer-driven pull)
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
