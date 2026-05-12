# Constellation v0.3.0 — Architecture (Draft)

**Status:** DRAFT — design locked, implementation in progress
**Date:** 2026-05-10
**Companion to:** Mem-Fusion v0.1.0+ ([github.com/muycoreano/mem-fusion](https://github.com/muycoreano/mem-fusion))

---

## 1. Mission

**Constellation enables group memory across multiple Mem-Fusion nodes.** Where Mem-Fusion gives Claude long-term memory across sessions on one machine, Constellation lets that memory federate across machines — sharing curated knowledge between team members' AI agents while preserving each member's personal memory privacy.

Constellation is bundled with Mem-Fusion (lives in the same repo at `src/`) but runs as a **separate daemon process** with its own MCP server, its own storage, and its own network endpoint. The two products serve different purposes:

| | Mem-Fusion | Constellation |
|---|---|---|
| Purpose | Personal memory (cross-session, single machine) | Group memory (across machines) |
| Transport | stdio MCP, spawned per Claude session | HTTP MCP, persistent daemon |
| Network exposure | Localhost only | Network-bound for inter-peer reachability |
| Storage | Local Qdrant collection (`mem_fusion_memories`) | Separate Qdrant collection (`mem_fusion_canonical_memories`) |
| Lifecycle | Ephemeral (Claude subprocess) | Always-on (launchd-managed) |
| Tool surface to local Claude | All 8 memory tools | All 4 constellation tools |
| Tool surface to remote peers | None (not reachable) | All 4 constellation tools |

**Critical safety property:** Constellation has no privileged access to Mem-Fusion's local memory. The two daemons share Qdrant infrastructure but operate on disjoint collections. Cross-process boundaries enforce isolation; there is no API path from "remote peer makes a request" to "local memory store."

---

## 2. Object Model

Two entities. One role.

### Node

A participant in the constellation system. Each Mem-Fusion install on a machine corresponds to one node.

```
node:
  name             string         e.g., "alice-mac"
  endpoint         URL            e.g., "http://10.0.0.5:7433"
  memberships      list of group memberships (group_name + role)
```

A node has stable identity (its address + name); its *role* (orchestrating vs. non-orchestrating) varies per group.

### Group

A named federation of nodes that share canonical memory.

```
group:
  name                    string         e.g., "engineering@branch"
  orchestrating_node      Node           exactly one
  members                 list of Node   non-orchestrating members
  canonical_memory        store          this group's shared memory (in orchestrator's Qdrant collection)
```

A group has exactly one orchestrating node and zero or more non-orchestrating member nodes. The orchestrating node holds the canonical memory store for the group.

### The Role

"Orchestrating" is **not a type of node** — it's a **per-group role** that a node plays for a specific group. The same node can be:

- A non-orchestrating member of group A
- The orchestrating node of group B
- A non-orchestrating member of group C

…all simultaneously. Three memberships, one orchestrating role. Identity stays at the node; role is contextual.

### What this dissolves

The model has no "broker," "relay," "bridge," "federation server," or "peer-to-peer mesh" entities. Those concepts collapse into either:
- A node (the actor)
- A group (the federation unit)
- A membership (the relationship)

If a request flows from one group to another, it does so because a node with memberships in *both* groups made an explicit, deliberate call. No special entity, no third-party intermediation.

---

## 3. The Routing Rules

These four invariants are the load-bearing logic of the system. Every protocol operation respects them.

### Rule 1: Direct send only

A node sending a memory to a group calls that group's orchestrating node *directly*. No intermediation, no relay, no path discovery.

### Rule 2: Membership equals authorization

A node can only send memory to (or query memory from) groups it is a member of. There is no "send to group X without being in X" pathway. Membership IS the permission.

### Rule 3: No transitive routing

A node in group A cannot send to group B "via" group A's orchestrator, even if A's orchestrator happens to also be in B. The originating actor must be a direct member of the target group.

### Rule 4: No automatic propagation

An orchestrating node's membership in a parent group is *descriptive*, not *active*. Memory put into group A does NOT automatically propagate to any group A's orchestrator may belong to. Propagation is always an explicit, deliberate second call by a node with multi-group membership.

### Consequences

- **Routing is degenerate.** Every memory operation is a one-hop call from a member to that group's orchestrating node. There is no routing protocol because there is no routing problem.
- **Cost is O(1) per memory by default.** Memory storage cost = one canonical copy in the originating group. Multi-group replication only happens when someone explicitly chooses to incur it.
- **Trust surface is bounded.** Each node verifies only its orchestrating nodes (constant, small N), not other nodes (which would be O(N²) in a mesh).
- **Audit trail is built in.** When a memory crosses groups, it does so via an explicit `memory/put` call by a known multi-membership node. Provenance is recorded in the new copy.

---

## 4. Protocol Surface

Four tools. All scoped by group. All under the `constellation/` namespace.

### `constellation/memory/put`

Submit a memory to a group's canonical store.

```
inputs:
  group_name      string       which group to put into
  content         string       the memory text
  type            enum         decision|fact|preference|error|code|context|session
  tags            list[string] optional topic tags
  importance      int          1-5
  provenance      object       optional; origin metadata (used when promoting from another group)

returns:
  status:    stored | duplicate | rejected
  id:        UUID of stored memory (if stored)
```

Authorization: caller MUST be a member of `group_name`. Orchestrating node validates membership before accepting.

### `constellation/memory/get`

Fetch memory from a group's canonical store.

```
inputs:
  group_name      string             which group to query
  id              optional string    specific memory by ID
  filters         optional object    since, tags, type, importance threshold
  query           optional string    semantic search query

returns:
  list of memory records
```

Authorization: caller MUST be a member of `group_name`.

### `constellation/peers`

List all members of a group, including the orchestrating node.

```
inputs:
  group_name      string

returns:
  group:          name + orchestrating_node + members[]
  each member:    {name, endpoint, role, last_seen}
```

Authorization: caller MUST be a member of `group_name`.

### `constellation/peers/self`

Return the calling node's own info — its identity and all its memberships.

```
inputs:  none

returns:
  name:           string
  endpoint:       string
  memberships:    [{group_name, role}]
```

Authorization: none — every node can query its own info.

### What is NOT in the protocol

The following operations are deliberately absent. Their absence is part of the design:

- **No `broker_request`** — cross-group flow happens by explicit `memory/put` calls by multi-membership nodes, not a special broker verb.
- **No `subscribe` / `webhook` / `notify` in v0.3.0** — pull-only. Push semantics (subscribe + webhook delivery) added in post-MVP. See §5 Synchronization.
- **No `delete` / `revoke`** — append-only model for canonical memory. post-MVP may add explicit retraction.
- **No `promote` / `elevate`** — promotion is just a second `memory/put` call by a multi-membership node. No special verb.
- **No `role/transfer` / `elect`** — orchestrating node is configured statically; dynamic transfer deferred to post-MVP.

---

## 5. Synchronization

How memories move between peers and the orchestrator. Defines pull, push, watermarks, the promotion queue, and offline behavior.

### 5.1 Sync model

Constellation is **pull-primary, push-augmented**. Peers read canonical memory from the orchestrator on a schedule (or on demand); the orchestrator can additionally notify peers about new memories in real time when push is enabled.

The peer chooses its sync mode in `config.json`:

```json
{
  "sync": {
    "mode": "manual" | "poll" | "push" | "push+poll",
    "poll_interval_seconds": 30,
    "auto_apply": true,
    "auto_promote": true,
    "failure_verbosity": "verbose"
  }
}
```

| Mode | Description | Available |
|---|---|---|
| `manual` | No background sync. User/Claude calls `constellation/pull_new` when desired. | v0.3.0 |
| `poll` | Auto-pull scheduler runs every `poll_interval_seconds`. | v0.3.0 |
| `push` | Peer subscribes to orchestrator webhooks; pulls on receipt. | post-MVP |
| `push+poll` | Both. Push for real-time; poll as fallback for missed webhooks. | post-MVP |

`auto_apply: true` writes pulled canonicals into the peer's local mem-fusion Qdrant automatically. `auto_promote: true` (the v0.3.0 default) automatically promotes every user-initiated `store_memory` call to the orchestrator for groups this peer is a member of.

### 5.2 Pull primitives

Two endpoints. Both pull-direction (peer initiates HTTP call to orchestrator). Watermark is a microsecond-precision ISO-8601 timestamp on the canonical's `received_at` field.

#### `GET /memory/since` — lightweight count check *(v0.3.0)*

Returns the count of canonicals newer than a watermark, without payloads. Used for cold-boot count display, polling probes, and "is anything new?" checks.

```
GET /memory/since?group_name=<str>&since=<iso8601>?

→ 200 {
    group_name:         str,
    since:              str | null,
    count:              int,
    latest_received_at: str | null
  }
```

If `since` is omitted, returns the total count of canonicals in the group. Useful for fresh peers with no prior watermark.

#### `GET /memory/get` extended with `since` *(v0.3.0)*

The existing endpoint gains an optional `since` parameter that filters `received_at >= since`. Results are sorted by `received_at` ascending so peers can apply them in order and advance their watermark cleanly.

```
GET /memory/get?group_name=<str>&since=<iso8601>&...
```

Combines with `id` (still wins if both provided) and future semantic-search `query`. Pagination via continuation cursor deferred to a future version.

### 5.3 Watermarks and cold start

Each peer maintains a `last_synced_at` watermark per group, persisted in `peer_state.json` (§5.5). On sync:

```
last_applied = last_synced_at
for memory in pull_response.memories:  # already sorted by received_at asc
    upsert(memory)                      # idempotent by canonical_id
    last_applied = max(last_applied, memory.received_at)
last_synced_at = last_applied
```

**Cold start** (no prior `last_synced_at`): peer omits the `since` parameter, pulls the full corpus for the group, applies in order, sets the watermark to the highest `received_at` returned. From there, every subsequent pull is incremental.

### 5.4 Push notifications *(post-MVP)*

When a new canonical lands via `/memory/put`, the orchestrator fires a webhook to every active subscription for that group.

#### `POST /peers/subscribe`

```
body: {
  group_name:      str,
  callback_url:    str,
  callback_secret: str?       // HMAC secret for body verification
}

→ 200 {
    subscription_id: str,
    registered_at:   str
  }
```

The orchestrator sends a synchronous `subscription.test` event to verify the callback URL is reachable; on failure rolls back the subscription and returns 502.

#### `DELETE /peers/subscribe` and `GET /peers/subscriptions`

Unsubscribe and list-active. Standard CRUD surface.

#### Webhook delivery to the peer

```
POST <callback_url>
Headers:
  Content-Type: application/json
  X-Constellation-Event:        memory.put | subscription.test
  X-Constellation-Signature:    hmac-sha256-hex(callback_secret, body)
  X-Constellation-Subscription: <subscription_id>
  X-Constellation-Delivery:     <unique_delivery_id>    // for idempotency

body: {
  event:         "memory.put",
  group_name:    str,
  canonical_id:  str,
  content_hash:  str,
  origin_node:   str,
  received_at:   str,
  summary:       str    // first ~80 chars of content
}
```

**Delivery semantics:**
- **At-least-once.** Idempotency via `X-Constellation-Delivery` header — peer dedupes.
- **Asynchronous.** `/memory/put`'s response is not blocked on webhook delivery.
- **Retry schedule:** 1m, 5m, 30m, 2h, 12h (5 attempts). After all fail, subscription is marked degraded; peer can re-subscribe to reset.
- **Eventual consistency:** Even with complete webhook failure, the peer's pull cycle recovers via `/memory/since` + `/memory/get?since=...`.
- **No ordering guarantee.** Webhooks for puts A and B may arrive in either order. Peer applies idempotently using `canonical_id` as the key.

### 5.5 Peer state file

`<state_dir>/peer_state.json`. Atomic write (temp + rename). One entry per group.

```json
{
  "schema_version": 1,
  "groups": {
    "wp2-test-group@dev": {
      "orchestrator_url":       "http://127.0.0.1:7533",
      "last_synced_at":         "2026-05-12T02:19:03.938230Z",
      "last_check_at":          "2026-05-12T02:30:00.000000Z",
      "pending_count":          0,
      "orchestrator_reachable": true,
      "consecutive_failures":   0,
      "last_failure_at":        null,
      "last_failure_reason":    null,
      "subscription_id":        null,
      "callback_url":           null
    }
  }
}
```

`last_synced_at` is the sync cursor. `last_check_at` is the most recent `/memory/since` call (separate from the watermark). `pending_count` is the latest known delta — what SessionStart surfaces.

### 5.6 Auto-promotion

`auto_promote: true` (v0.3.0 default) means every `store_memory` call automatically attempts to push the new memory to the orchestrators of every group the peer is a member of. This makes group memory sharing the path of least friction — the user says "remember X," mem-fusion stores locally AND promotes.

The store response always includes a `promoted_to` array showing per-group outcomes:

```json
{
  "status":   "stored",
  "local_id": "026bfa91-...",
  "promoted_to": [
    {"group_name": "engineering@branch", "status": "stored",     "canonical_id": "e98037dc-..."},
    {"group_name": "mobile@branch",       "status": "queued",     "reason":       "ConnectionRefusedError"}
  ]
}
```

Statuses: `stored`, `duplicate`, `queued` (transient failure, will retry), `queue_full` (queue at cap, see §5.7), `failed` (4xx from orchestrator — non-retryable).

### 5.7 Promotion queue

When auto-promote can't reach the orchestrator (network error or 5xx), the promotion is written to `<state_dir>/queue/<group>/<timestamp>-<content_hash>.json`:

```json
{
  "queued_at":           "2026-05-12T02:30:00.123456Z",
  "group_name":          "wp2-test-group@dev",
  "orchestrator_url":    "http://127.0.0.1:7533",
  "memory_record":       { ... full /memory/put body ... },
  "provenance":          { ... },
  "attempt_count":       0,
  "last_attempt_at":     null,
  "last_failure_reason": null
}
```

**Queue limits:**
- **Per-peer max: 100 queued entries.** Practical maximum at expected ~5-10 promotions/day = ~2 weeks of orchestrator outage.
- **Behavior when full:** new promotions are rejected with `status: "queue_full"` in the `store_memory` response. Local memory is still stored. User/Claude sees the cap loudly and can investigate (orchestrator down? misconfigured? legitimate outage?).
- **Orphaned group queues** (queue files for a group no longer in `config.json` memberships): left alone. No auto-cleanup in v0.3.0. Manual `rm -rf` if it ever matters.

The queue is Constellation-owned and lives separately from mem-fusion's existing `~/.local/share/mem-fusion/queue/` (which handles Ollama-fallback for the local store). The two queues drain on different conditions.

### 5.8 Drain semantics

The same scheduler that runs pull cycles also drains the promotion queue.

```
Every poll_interval_seconds:
  1. Pull cycle (§5.2)
     - If orchestrator unreachable, record failure in peer_state.json,
       skip drain for this group this cycle.
  2. Drain cycle:
     For each <group>/*.json in queue dir:
       POST orchestrator_url/memory/put with the queued body
       ├─ 200  (stored or duplicate)  → delete queue file
       ├─ 4xx  (real error)            → move to queue/failed/<group>/, log,
       │                                  surface in next SessionStart
       └─ 5xx / network                → leave in queue, increment attempt_count,
                                          retry next cycle
  3. Update peer_state.json atomically
```

**Drain triggers:**
- Every `poll_interval_seconds` (scheduled)
- Opportunistic: immediately after a successful pull (suggests the orchestrator just came back)
- Manual: `constellation/sync_now` MCP tool

**No exponential backoff for v0.3.0.** Fixed cadence. If failure rate becomes a real problem (consecutive_failures > 60, ~30 minutes of failures), SessionStart surfaces a clear warning so the user can act.

### 5.9 Offline behavior and failure surfacing

**Every failed delivery attempt is communicated to the user/Claude.** This is intentional instrumentation for v0.3.0 and post-MVP — we don't yet have empirical data on baseline network reliability, so we err on the side of loud rather than silent. Silent retries can mask real bugs.

Four channels surface failures:

| Channel | When | Carries |
|---|---|---|
| Synchronous `store_memory` response | Immediate, same MCP call | `promoted_to: [{group, status, reason?}]` per group |
| `peer_state.json` | Updated every pull/drain cycle | `consecutive_failures`, `last_failure_at`, `last_failure_reason` per group |
| SessionStart hook (mem-fusion) | At the start of every Claude session | Aggregated state from `peer_state.json` and queue dir |
| `<state_dir>/logs/peer-client.log` | Continuous append from the scheduler | Every drain attempt's outcome with full reason |

A failure doesn't vanish in one channel — it's persisted in state and shows up at session start until resolved.

**Future configurability.** Once stability has been empirically demonstrated (likely after a multi-week real deployment with logged failure baselines that look like noise rather than signal), the noise can be tuned:

```json
{
  "sync": {
    "failure_verbosity": "verbose" | "summary" | "errors-only" | "silent"
  }
}
```

For v0.3.0 and post-MVP the value is fixed at `verbose`. Promoting `summary` or `errors-only` to a release is a deliberate decision made against observed failure-rate data, not a default to enable preemptively.

### 5.10 Known limitations

- **Same-microsecond `received_at` collisions.** Two canonicals landing in the exact same microsecond would be indistinguishable by the `since=<µs>` cursor — a peer could miss one. For a single-threaded HTTP daemon serializing PUTs (the v0.3.0–post-MVP design), this is structurally impossible: each PUT path takes hundreds of microseconds to single-digit milliseconds, so microseconds give ~1000× headroom. The limitation surfaces only if the daemon ever becomes multi-threaded or if multiple orchestrators serve the same group with clock skew. Mitigation at that point: per-group monotonic `sequence_number` payload field as the internal ordering primitive, with timestamps remaining the wire-protocol cursor.
- **No bulk-fetch pagination.** A peer offline a month coming back to a large canonical could receive a multi-MB `/memory/get?since=...` response. Acceptable for current scale (≤500 canonicals per group, ~6KB each). Pagination via continuation cursor is reserved for a future version when real workloads hit it.
- **No automatic queue cleanup on membership change.** Removing a group from `memberships` leaves any pending promotions in `<state_dir>/queue/<group>/` untouched. Manual cleanup.
- **Webhook delivery is best-effort.** After 5 retries spanning ~15 hours, the orchestrator gives up on a webhook subscription. Peers must re-subscribe to recover. Pull-fallback remains the durable path; webhooks are an optimization for real-time, not the system of record.

### 5.11 MCP tool surface for sync

Peer-side MCP tools, in addition to the existing four protocol tools:

| Tool | Args | Returns | Version |
|---|---|---|---|
| `constellation/check_new` | `group_name` | `{count, since, latest_received_at}` — uses peer's current watermark | v0.3.0 |
| `constellation/pull_new` | `group_name`, `auto_apply?` | `{count_pulled, applied, new_watermark}` | v0.3.0 |
| `constellation/sync_status` | `group_name?` | Per-group state from `peer_state.json` + queue summary | v0.3.0 |
| `constellation/sync_now` | `group_name?` | Force immediate pull + drain cycle. Returns same shape as `sync_status`. | v0.3.0 |
| `constellation/subscribe` | `group_name` | `{subscription_id}` — registers webhook, starts listener if needed | post-MVP |
| `constellation/unsubscribe` | `group_name` | `{ok}` | post-MVP |

Tools read `last_synced_at` from `peer_state.json` automatically. Claude doesn't pass watermarks explicitly.

### 5.12 Cold-boot walk-through

Peer-c boots up after 3 hours offline.

```
1. launchd loads com.branchapp.memfusion.constellation-pull
   (RunAtLoad=true, StartInterval=poll_interval_seconds)
2. constellation_pull.py first run:
   a. Read config.json → memberships, sync settings
   b. Read peer_state.json → last_synced_at per group
   c. For each peer-role group:
      GET orchestrator_url/memory/since?since=<watermark>
        → {count: 7, latest_received_at: "2026-05-12T05:30:00.000Z"}
      If auto_apply:
        GET orchestrator_url/memory/get?since=<watermark>
        Apply 7 records to local Qdrant (mem_fusion_memories)
        Advance last_synced_at = max(received_at)
   d. Drain queue (§5.8). No queued entries for this group → noop.
   e. Write peer_state.json atomically.
3. User opens Claude Code at some point later
4. SessionStart hook reads peer_state.json + queue dir, surfaces:
     <memfusion_status>
       Group memory: 7 new in wp2-test-group@dev (auto-applied)
       Recent activity: ...
     </memfusion_status>
5. Claude has 7 new memories in semantic-search range. User asks
   "what did the team learn while I was away?" → search returns the new
   content tagged source=constellation-pull / origin_node=mem-fusion-peer-b.
```

When the orchestrator is unreachable at boot:

```
1-2 as above
2c. GET /memory/since fails → record orchestrator_reachable=false,
    last_failure_reason="ConnectionRefusedError"
2d. Drain skipped (orchestrator unreachable).
3. User opens Claude Code
4. SessionStart surfaces:
     <memfusion_status>
       Group memory:
         wp2-test-group@dev: orchestrator unreachable for 14 min
         (last error: ConnectionRefusedError)
         Queued promotions: 2 — will retry on next sync cycle (30s)
     </memfusion_status>
5. Claude knows about the outage. User can call constellation/sync_now
   to attempt an immediate retry, or wait for the scheduled cycle.
```

### 5.13 Storage hygiene — where pulled memories live on the peer

Pulled canonicals are written into the peer's local `mem_fusion_memories` collection — the same Qdrant collection that holds personal memories. They are not segregated into a separate group-only collection.

**Tagging:** every pulled canonical's payload includes:
- `source: "constellation-pull"`
- `group_name: <group>`
- `origin_node: <node_name>` (carried over from the canonical's provenance)

**Why same collection:** semantic search returns both personal and group content together, ranked by relevance. The user (or Claude) is searching for *what they need*, not *which store it came from*. The tag enables optional filtering (`source=constellation-pull` to see only group content) without making "find the relevant memory" require a multi-store query.

**Idempotency:** the local upsert uses `canonical_id` as the point id. Re-running a pull (after a crash, after a retry) is a no-op for any memory already applied.

---

## 6. Process Architecture

Two daemons, one machine.

```
LOCAL MACHINE
│
├── Qdrant                        (shared infrastructure)
│   ├── mem_fusion_memories               ← Mem-Fusion's collection (personal)
│   └── mem_fusion_canonical_memories     ← Constellation's collection (group)
│
├── Ollama                        (shared embedding service)
│
├── Mem-Fusion daemon             ◄── PROCESS 1
│   • MCP transport: stdio (per-session subprocess spawned by Claude)
│   • Bound to: nothing (no network)
│   • Tool surface: all 8 memory tools, local-only
│   • Storage: mem_fusion_memories
│   • Lifecycle: spawned per Claude session, killed at session end
│   • UNCHANGED from v0.1.0
│
└── Constellation daemon          ◄── PROCESS 2 (NEW in v0.3.0)
    • MCP transport: HTTP/SSE, persistent
    • Bound to: configured network interface (default 127.0.0.1 for v0.3.0; LAN/WAN later)
    • Tool surface: constellation/memory/put|get, peers, peers/self
    • Storage: mem_fusion_canonical_memories
    • Lifecycle: managed by launchd (com.branchapp.memfusion.constellation)
    • Access control: localhost-only by default; expanded with explicit network bind
```

### Why two daemons

1. **Lifecycle mismatch.** Mem-Fusion only needs to exist while Claude is using it (stdio subprocess). Constellation needs to be reachable when no local Claude is active (persistent daemon).

2. **Process isolation.** Constellation has no in-process access to Mem-Fusion's memory tools. A bug or compromise in Constellation cannot corrupt or exfiltrate local memory.

3. **Independent deployment.** Each daemon can be upgraded, restarted, or disabled independently. Users can run Mem-Fusion-only for personal memory or Constellation-only for federation gateway deployments.

4. **Clean security model.** Network exposure is opt-in by enabling Constellation. Mem-Fusion has zero network footprint, ever.

### Claude's view

Claude on the local machine connects to BOTH daemons as MCP servers:

```
claude mcp list:
  mem-fusion        stdio    <command>  ✓ Connected
  constellation     http     http://127.0.0.1:7433  ✓ Connected
```

The user (via Claude) is the bridge. To share a memory to a group: explicitly call both `mem-fusion`'s `store_memory` (local copy) and `constellation`'s `memory/put` (group canonical). No automatic propagation between the two.

---

## 7. Storage Model

Each group's canonical memory lives in **one Qdrant collection on the orchestrating node's machine**.

For a group orchestrated by node `eng-mgr-mac`:
- Collection: `mem_fusion_canonical_memories` (with `group_name` payload field)
- Or: per-group collections like `mem_fusion_canonical_engineering`

(v0.3.0 implementation choice TBD: single collection with `group_name` filter vs. one collection per orchestrated group. Single-collection is simpler; per-group is cleaner. Decide at implementation time.)

Memory records carry these payload fields:

```
{
  "content":         string,
  "type":            enum,
  "tags":            list[string],
  "importance":      int,
  "group_name":      string,             which group this belongs to
  "submitted_by":    node_name,          which node originally submitted
  "submitted_at":    ISO timestamp,
  "provenance":      optional object,    if promoted from another group:
                                         { "origin_group": "...", "promoted_by": "...", "promoted_at": "..." }
  "content_hash":    string              for dedup
}
```

The orchestrating node owns this storage and is the single source of truth for the group's canonical memory.

### Multi-orchestration

A node that orchestrates multiple groups serves multiple canonical stores. Each group's storage is namespaced (via collection or payload filter). The node's Constellation daemon handles requests for any of its orchestrated groups, dispatching to the right backing store.

---

## 8. Authentication & Authorization

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

## 9. Lifecycle

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

## 10. v0.3.0 Scope Constraints

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

## 11. Forward Compatibility

The v0.3.0 protocol is designed so that post-MVP extensions don't break existing nodes:

1. **All operations take `group_name`.** In v0.3.0 there's only one group, so this is trivial. In post-MVP the same call shape supports multi-group nodes — pick which group the call targets.

2. **`peers/self` returns `memberships` as a list.** v0.3.0 returns a list of length 1; post-MVP returns multiple entries.

3. **Memory records carry `provenance`.** Empty in v0.3.0 (no cross-group flow exists); populated in post-MVP when memories are promoted between groups.

4. **Config schema supports multiple memberships and orchestrating entries.** v0.3.0 uses length-1 lists; post-MVP extends naturally.

5. **The protocol has no special verb that becomes obsolete.** Cross-group propagation in post-MVP uses the same `memory/put` primitive applied recursively at multi-membership nodes.

No anticipated breaking change at the protocol level between v0.3.0 and v1.0.

---

## 12. Examples

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

## 13. Open Questions

The following are known unresolved decisions. They don't block v0.3.0 implementation but should be settled before v0.3.0 ships:

1. **Storage layout: single collection vs. per-group collection?** Single is simpler; per-group is cleaner for isolation and lifecycle. Implementation will choose; either preserves the protocol.

2. **Swarm key rotation.** v0.3.0 has no key rotation mechanism. If a swarm key is compromised, the group's only recourse is to generate a new one and redistribute. post-MVP should add a key-rotation primitive.

3. **Snapshot semantics for `memory/get` with no filters.** Returns ALL memories? Paginated? Capped at N? Decide based on expected canonical store size (likely capped at 1000 with a `cursor` pagination field).

4. **What happens when a member's swarm key is removed from the orchestrator's allow-list mid-session?** Should in-flight requests complete or fail? Likely: in-flight completes; new requests fail. Worth confirming.

5. **Should Constellation's persistent daemon survive Mem-Fusion uninstall?** Currently they're independent; uninstalling Mem-Fusion leaves Constellation running. May be desirable (gateway deployments) or surprising (user expected full cleanup). Decide UX.

6. **Cross-platform launchd-equivalent.** Constellation depends on launchd (macOS) for daemon management. Linux/Windows support requires systemd / Windows Services equivalents. Out of v0.3.0 scope but worth scoping later.

---

## 14. What Constellation is NOT

Closing with a clear list of things Constellation deliberately is not, so future contributors don't try to bend it into them:

- **Not a peer-to-peer mesh.** All relationships flow through orchestrating nodes.
- **Not a generic message bus.** The protocol is scoped to memory federation; Constellation is not Kafka or NATS.
- **Not a workflow engine.** Constellation moves memory between groups; it does not run scheduled jobs or trigger external integrations.
- **Not a cloud service.** Constellation runs locally on each member's machine. No SaaS, no cloud component.
- **Not Mem-Fusion.** Mem-Fusion is the substrate for personal memory; Constellation is the substrate for group memory. Same repo, distinct daemons, complementary purposes.

---

*Draft architecture document · 2026-05-10 · awaiting review and ratification*
