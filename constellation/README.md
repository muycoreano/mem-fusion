# Constellation

**Experimental peer-to-peer extension for Mem-Fusion.**

Constellation is the optional P2P sibling system that implements Mem-Fusion's sharing API for direct machine-to-machine memory sync. It exists for users who want to share memories without going through cloud productivity tools — corporate environments where Slack/Drive/Teams aren't approved for memory content, fully-private meshes, offline / air-gapped setups, or anyone who prefers a self-hosted federation.

For most users who can use a cloud productivity tool as a substrate, the [Connectors](../README.md#connectors-preview-v05) model in mem-fusion native is the simpler path. Constellation is for the cases where Connectors don't fit.

---

## Where Constellation fits

Mem-Fusion has a local memory layer and a sharing API. Two distinct sibling systems implement the sharing API:

```
              Mem-fusion LOCAL
              (Qdrant + Ollama + MCP)
                     │
       shared API: push/pull/status/since
                     │
        ┌────────────┴────────────┐
        ▼                         ▼
  Connectors                Constellation
  (cloud productivity       (P2P sibling)
   tools — slack first)
```

**Mem-fusion native (Connectors)** targets cloud productivity tools — Slack, Google Drive, Teams, Discord, Notion. The productivity tool's substrate handles auth and access control.

**Constellation** targets a configured peer mesh over LAN HTTP. Authentication is by group membership agreed out-of-band; auth boundary is your network.

Both implement the same `push`/`pull`/`status`/`since` contract. A user can run neither, either, or both simultaneously.

---

## Layered architecture inside Constellation

Constellation itself has two layers:

1. **Constellation API (outer)** — Constellation's implementation of mem-fusion's sharing API. The thing mem-fusion calls when a user asks to push or pull through Constellation.

2. **P2P backend (inner, discriminated by `p2p_type`)** — the actual peer-to-peer protocol underneath. The v0.4 HTTP daemon is the bundled backend (`p2p_type: "http"`). Future backends could include cassandra-backed clustering, libp2p, or other P2P substrates — they plug into the same Constellation API.

The `p2p_type` field defaults to `"http"` when absent. Every existing Constellation config is a valid Constellation config under this model — strict additive change, no migration required.

```
          Mem-fusion sharing API call
                     │
                     ▼
          ┌──────────────────────┐
          │  Constellation API   │
          │  (push/pull/status/  │
          │   since)             │
          └──────────┬───────────┘
                     │
        ┌────────────┴───────────┐
        │   p2p_type discriminator│
        └────────────┬───────────┘
                     │
        ┌────────────┼───────────────┐
        ▼            ▼               ▼
   p2p_type:    p2p_type:        p2p_type:
     http       cassandra         libp2p
   (current)    (future)         (future)
```

The current HTTP backend is what's shipping; the architectural layer below is designed so other P2P substrates can plug in without changing Mem-Fusion's surface.

---

## Install

Follow [`INSTALL_CONSTELLATION.md`](../INSTALL_CONSTELLATION.md) at the repo root. Quick summary:

1. Install Mem-Fusion first (see top-level [`README.md`](../README.md#install)).
2. Open Claude Code.
3. Copy contents of [`INSTALL_CONSTELLATION.md`](../INSTALL_CONSTELLATION.md).
4. Paste into Claude Code and approve all subsequent steps.

Constellation runs as a persistent HTTP daemon (port 7533) with `p2p_type` defaulting to `"http"`. Memories from configured peers will be accessible automatically as if they were local.

---

## How the HTTP backend works (`p2p_type: "http"`)

```
   ┌──────────────────────────────────────────────────────┐
   │              PEER A (your laptop)                    │
   │   Mem-Fusion + Constellation + local Qdrant          │
   │   ─────────────────────────────────────────────      │
   │   store_memory(..., groups=[engineering])            │
   │     → local entry, origin_node=A, submitted_at=now   │
   └────────────────────────┬─────────────────────────────┘
                            │
                  POST /memory/put { groups: [...] }
                  (push-time filter strips groups
                   recipient isn't a member of)
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
   ┌─────────────────────┐    ┌──────────────────────┐
   │  PEER B (desktop)   │◄──►│  PEER C (teammate)   │
   │                     │    │                      │
   │  same stack         │    │  same stack          │
   │  global dedup       │    │  global dedup        │
   │  on content_hash;   │    │  on content_hash;    │
   │  additive merge of  │    │  additive merge of   │
   │  incoming groups    │    │  incoming groups     │
   └─────────────────────┘    └──────────────────────┘
```

Every peer runs the same stack: Mem-Fusion + Constellation daemon + local Qdrant. When you store a memory on Peer A tagged for a shared group, Constellation sends the full record (content + 768-dim vector + `content_hash` + filtered `groups` list) over HTTP to every peer in that group.

Receivers dedup globally on `content_hash` and additively merge incoming groups into existing entries — same content from a different group widens the `groups` list rather than creating a duplicate.

Group-shared entries live in the same Qdrant collection as local memories, so your Claude sees a teammate's stored decision as just another memory, with the original `origin_node` preserved end-to-end. A memory bob pushed and alice relayed to carol still says `origin_node=bob`.

There's no orchestrator, no privileged peer, no hub: every peer is symmetric. Any responsive peer in a group can serve the full group history on `/memory/since` pull — so downtime of any single peer doesn't lose data. Trust is by group membership, agreed out-of-band.

---

## Configuring Constellation

Constellation's config lives at:

```
~/.local/share/mem-fusion/constellation/config.json
```

Open it in any editor; it looks like this:

```json
{
  "node_name": "alice-mac",
  "p2p_type":  "http",

  "peer_listen_address":    "0.0.0.0:7533",
  "gateway_listen_address": "127.0.0.1:7534",
  "qdrant_url": "http://127.0.0.1:6333",
  "state_dir":  "~/.local/share/mem-fusion/constellation",

  "memberships": [
    {
      "group_name": "personal",
      "peers": []
    },
    {
      "group_name": "engineering@branch",
      "peers": [
        { "node_name": "bob-mac",      "endpoint": "http://10.0.0.5:7533" },
        { "node_name": "carol-laptop", "endpoint": "http://10.0.0.7:7533" }
      ]
    }
  ]
}
```

**`p2p_type` is optional** — defaults to `"http"` if absent. Existing Constellation configs without the field are valid under this schema. New configs should include it explicitly to make the backend choice clear.

Every install gets a `personal` membership pre-populated with empty peers — it's the implicit default for any memory not explicitly tagged for a shared group. To sync your own machines (laptop ↔ desktop), populate `personal.peers` with your other devices' endpoints.

### Add a teammate-group peer

1. Append to the relevant group's `peers[]` with the new peer's `node_name` and HTTP endpoint (their machine's IP/hostname on port 7533).
2. Restart the daemon:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.constellation.plist
   launchctl load   ~/Library/LaunchAgents/com.branchapp.memfusion.constellation.plist
   ```
3. Confirm: `curl http://127.0.0.1:7533/health` should return `ok: true` and list every configured group in `memberships`.
4. Ask Claude to `/remember pull` — your local store catches up on anything the new peer has already shared.

**Symmetric mesh:** every peer in a group needs every other peer in their `peers[]` list. There's no automatic discovery. If you add a 4th peer (`dave`), then alice, bob, and carol each add `dave` to engineering's peer list, and dave's config lists all three.

### Add a new group

Append a new `{group_name, peers}` entry to `memberships[]` and restart. Constellation supports any number of memberships per peer — `personal`, one or more team groups (`engineering@branch`, `product@branch`), project-specific groups (`design-system@frontend`), whatever the organization needs.

### Remove a peer

Delete the entry from `peers[]` and restart. Memories previously shared with that peer remain in your local Qdrant — removal only stops future pushes/pulls from them. Note: there is no "un-share" — peers that already received content keep it.

### Check that it's working

After any config change:

```bash
curl -s http://127.0.0.1:7533/peers/self        | python3 -m json.tool
curl -s -X POST http://127.0.0.1:7534/pull -d '{}' -H 'Content-Type: application/json' | python3 -m json.tool
```

The first shows your identity + every configured group membership. The second pulls from every group's peers and reports per-(peer, group) status — handy for verifying a peer endpoint actually responds. Pass `{"group": "engineering@branch"}` to scope to one group.

---

## Group-keyed sharing semantics

When Constellation is installed, every memory carries a `groups: list[str]` tag (a list of group names) that determines which peers see it. The default is `personal` — local-only unless you populate `personal.peers` with your own other machines.

| Memory's groups | Where it lives | Who sees it |
|---|---|---|
| `["personal"]` (default) | This machine's Qdrant only | Only your Claude on this machine. With cross-machine personal sync configured, also your other personal-group machines. |
| `["personal", "engineering@team"]` | This machine + every engineering@team peer | Your machines + engineering teammates' machines |
| `["engineering@team"]` (no personal) | Engineering teammates only | Skips your personal-group machines if any |

### `/remember` examples with sharing

**Default (local-only)** — no group named:

```
You:   /remember always use uv for Python environments

Claude: ✓ Stored with groups=[personal]. Stays on this machine.
```

**Explicit group at store time** — store *and* push in one call:

```
You:   /remember We standardized on PostgreSQL 16 with logical replication
       for the auth service, for engineering@branch

Claude: ✓ Stored as decision (id: a7e3c2d1, groups=[engineering@branch]).
        ✓ Pushed to engineering@branch:
            - alice-desktop: stored
            - bob-mac:       stored
            - carol-laptop:  unreachable (connection refused)
```

**Store-now-share-later** — capture during work, decide audience after:

```
You:   "Let's pause and store what we learned this morning."
Claude: ✓ Stored 4 memories with groups=[personal].

You:   "Share those with engineering."
Claude: ✓ Added engineering@branch to 4 memories.
        ✓ Pushed to engineering@branch: 4 stored on bob, 4 stored on alice.

You:   "Also share them with product."
Claude: ✓ Added product@branch to 4 memories.
        ✓ Pushed to product@branch peers ONLY: 4 stored on dave, 4 on eve.
          (Engineering peers not re-contacted — already have them.)
```

**Scope rule:** push only contacts peers in the named group, even if the memory is also tagged for other groups. *"Share with product"* never reaches engineering peers. To push to multiple groups in one user turn, name them all explicitly.

---

## Constellation MCP tools

When Constellation is installed, Mem-Fusion exposes 2 additional MCP tools:

- **`group_pull(group?)`** — pull new memories from peers. Omit `group` to iterate every configured group with peers; pass `group=<name>` to scope to one. Returns per-peer telemetry.
- **`group_push(group, memory_ids?)`** — share local memories with one group's peers. Always scoped to a single group per call. Pass `memory_ids` for targeted pushes; omit for bulk push of every entry tagged with that group.

Both tools require Constellation. If Constellation isn't installed, they return `{"error": "constellation_not_installed"}` and local memory tools keep working independently.

`add_groups(memory_ids, groups)` (a local-memory tool) retroactively widens an existing memory's group set — additive only; un-sharing isn't supported because peers already received the content.

---

## Backward compatibility

Constellation is committed to **strict additive evolution** under v0.5:

- **Existing configs continue to work.** The new `p2p_type` field defaults to `"http"` when absent. No edits required to upgrade.
- **Existing daemons continue to run.** The HTTP peer protocol is unchanged.
- **Existing memories with `groups: [...]` deserialize cleanly.** The mem-fusion native side may treat that field as legacy/ignored, but Constellation continues to use group_name + peers as its routing model.
- **Existing MCP tool signatures are stable.** `group_pull`, `group_push`, `add_groups` keep their contracts.

When new `p2p_type` backends ship (cassandra, libp2p, others), they'll declare their own type-specific config fields under the `p2p_type` discriminator. Migrations are explicit per-backend; no existing setup gets disrupted.

---

## Uninstall

```bash
claude mcp remove constellation
launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.constellation.plist
rm ~/Library/LaunchAgents/com.branchapp.memfusion.constellation.plist
rm -rf ~/.local/share/mem-fusion/constellation
```

This removes the daemon and its config but leaves local mem-fusion memories intact. Your local Qdrant store and the mem-fusion MCP server keep working. To uninstall mem-fusion as well, see the top-level [README's uninstall section](../README.md#uninstall).

---

## Further reading

- Top-level [`README.md`](../README.md) — Mem-Fusion overview and Connectors preview
- [`docs/CONSTELLATION_ARCHITECTURE.md`](../docs/CONSTELLATION_ARCHITECTURE.md) — full architectural detail (peer protocol, source tagging, dedup)
- [`docs/v0.5_CONNECTOR_ARCHITECTURE.md`](../docs/v0.5_CONNECTOR_ARCHITECTURE.md) — mem-fusion native connector model (the cloud alternative)
- [`INSTALL_CONSTELLATION.md`](../INSTALL_CONSTELLATION.md) — install entrypoint
- [`CHANGELOG.md`](../CHANGELOG.md) — version history
