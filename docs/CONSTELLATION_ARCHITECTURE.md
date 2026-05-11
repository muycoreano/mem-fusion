# Constellation v0.3.0 — Architecture (Draft)

**Status:** DRAFT — design locked, implementation in progress
**Date:** 2026-05-10
**Companion to:** Mem-Fusion v0.1.0+ ([github.com/muycoreano/mem-fusion](https://github.com/muycoreano/mem-fusion))

---

## 1. Mission

**Constellation enables group memory across multiple Mem-Fusion nodes.** Where Mem-Fusion gives Claude long-term memory across sessions on one machine, Constellation lets that memory federate across machines — sharing curated knowledge between team members' AI agents while preserving each member's personal memory privacy.

Constellation is bundled with Mem-Fusion (lives in the same repo at `extensions/constellation/`) but runs as a **separate daemon process** with its own MCP server, its own storage, and its own network endpoint. The two products serve different purposes:

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
- **No `subscribe` / `webhook` / `notify`** — v0.3.0 is pull-only. Push semantics deferred.
- **No `delete` / `revoke`** — append-only model for canonical memory. v0.4+ may add explicit retraction.
- **No `promote` / `elevate`** — promotion is just a second `memory/put` call by a multi-membership node. No special verb.
- **No `role/transfer` / `elect`** — orchestrating node is configured statically; dynamic transfer deferred to v0.4+.

---

## 5. Process Architecture

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

## 6. Storage Model

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

## 7. Authentication & Authorization

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

### Deferred to v0.4+

- mTLS for cross-machine deployments
- Per-node identity (cryptographic keypair instead of shared swarm key)
- Per-tool capability tokens
- Rate limiting and quotas
- Human-review apprenticeship loop for inbound canonical submissions

---

## 8. Lifecycle

### Node startup

1. Read config from `~/.local/share/mem-fusion/extensions/constellation/config.json`.
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

For v0.3.0, `memberships` has length 1 and `orchestrating` is either empty (peer node) or has length 1 (the group's orchestrator). Multi-group membership and multi-orchestration are v0.4+.

---

## 9. v0.3.0 Scope Constraints

The following are **explicitly out of scope** for v0.3.0. The protocol is designed to extend cleanly into them later, but they are not implemented in this release.

| Capability | v0.3.0 | Deferred to |
|---|---|---|
| Multi-group membership per node | One group only | v0.4 |
| Hierarchy of orchestrating nodes | Single orchestrator per group, no parent | v0.4 |
| Cross-group memory propagation | N/A (only one group) | v0.4 |
| Dynamic orchestrator election | Statically configured | v0.4 or later |
| Human review / curator / apprenticeship loop | Auto-accept all valid submissions | v0.4 |
| Direct node-to-node communication | Forbidden — always through orchestrating node | Never (architectural invariant) |
| mTLS / per-node identity | Swarm key only | v0.4 |
| Cross-org federation | Single-org / single-constellation | v0.5+ |
| Rate limiting / quotas | None | v0.4+ |
| libp2p transport | Not used | Likely never (HTTP/HTTPS sufficient) |

### Why v0.3.0 ships small

The minimum architecture that **proves group memory works as a substrate**:
- Multiple Mem-Fusion installs federate via a shared canonical store
- Memory can be submitted by any member and read by any member
- The orchestrating node holds the authority
- The boundary between personal and group memory is real and enforced

Everything else is either (a) deferred because it can be added without breaking v0.3.0, or (b) deferred because we want to learn from v0.3.0 use before designing v0.4.

---

## 10. Forward Compatibility

The v0.3.0 protocol is designed so that v0.4 extensions don't break existing nodes:

1. **All operations take `group_name`.** In v0.3.0 there's only one group, so this is trivial. In v0.4 the same call shape supports multi-group nodes — pick which group the call targets.

2. **`peers/self` returns `memberships` as a list.** v0.3.0 returns a list of length 1; v0.4 returns multiple entries.

3. **Memory records carry `provenance`.** Empty in v0.3.0 (no cross-group flow exists); populated in v0.4 when memories are promoted between groups.

4. **Config schema supports multiple memberships and orchestrating entries.** v0.3.0 uses length-1 lists; v0.4 extends naturally.

5. **The protocol has no special verb that becomes obsolete.** Cross-group propagation in v0.4 uses the same `memory/put` primitive applied recursively at multi-membership nodes.

No anticipated breaking change at the protocol level between v0.3.0 and v1.0.

---

## 11. Examples

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

### Example 2: Forward-looking v0.4 — engineering, CTO, product hierarchy

**Setup (v0.4 capability, not v0.3.0):**

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

### Example 3: A node belonging to multiple groups (v0.4)

Alice is on engineering AND on a cross-functional task force.

```
alice's config (v0.4):
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

## 12. Open Questions

The following are known unresolved decisions. They don't block v0.3.0 implementation but should be settled before v0.3.0 ships:

1. **Storage layout: single collection vs. per-group collection?** Single is simpler; per-group is cleaner for isolation and lifecycle. Implementation will choose; either preserves the protocol.

2. **Swarm key rotation.** v0.3.0 has no key rotation mechanism. If a swarm key is compromised, the group's only recourse is to generate a new one and redistribute. v0.4 should add a key-rotation primitive.

3. **Snapshot semantics for `memory/get` with no filters.** Returns ALL memories? Paginated? Capped at N? Decide based on expected canonical store size (likely capped at 1000 with a `cursor` pagination field).

4. **What happens when a member's swarm key is removed from the orchestrator's allow-list mid-session?** Should in-flight requests complete or fail? Likely: in-flight completes; new requests fail. Worth confirming.

5. **Should Constellation's persistent daemon survive Mem-Fusion uninstall?** Currently they're independent; uninstalling Mem-Fusion leaves Constellation running. May be desirable (gateway deployments) or surprising (user expected full cleanup). Decide UX.

6. **Cross-platform launchd-equivalent.** Constellation depends on launchd (macOS) for daemon management. Linux/Windows support requires systemd / Windows Services equivalents. Out of v0.3.0 scope but worth scoping later.

---

## 13. What Constellation is NOT

Closing with a clear list of things Constellation deliberately is not, so future contributors don't try to bend it into them:

- **Not a peer-to-peer mesh.** All relationships flow through orchestrating nodes.
- **Not a generic message bus.** The protocol is scoped to memory federation; Constellation is not Kafka or NATS.
- **Not a workflow engine.** Constellation moves memory between groups; it does not run scheduled jobs or trigger external integrations.
- **Not a cloud service.** Constellation runs locally on each member's machine. No SaaS, no cloud component.
- **Not Mem-Fusion.** Mem-Fusion is the substrate for personal memory; Constellation is the substrate for group memory. Same repo, distinct daemons, complementary purposes.

---

*Draft architecture document · 2026-05-10 · awaiting review and ratification*
