---
name: mem-fusion-slack-connector
description: Set up, operate, and troubleshoot the mem-fusion Slack connector for cross-machine memory sharing.
trigger: User says "set up the Slack connector", "connect mem-fusion to Slack", "share memories via Slack", "why isn't my Slack push working", "add another Slack channel for mem-fusion", or asks about mem-fusion Slack-channel configuration.
---

# mem-fusion Slack connector — setup + operation + troubleshooting

## What this skill is and isn't

**Is:** the operational reference for mem-fusion's Slack connector specifically. Setup (first-time + add-another-channel), push/pull operation specifics, multi-connector management, troubleshooting failure modes.

**Isn't:** a generic `/remember` skill — that lives at [`~/.claude/skills/remember/SKILL.md`](~/.claude/skills/remember/SKILL.md) and handles the dispatch surface (`/remember push|pull <connector-id>`). When the user invokes a Slack-specific operation through `/remember`, the orchestration there delegates back to this skill for Slack-specific knowledge.

**Isn't:** a one-shot installer. Consulted any time Slack-connector work happens, including months after first setup.

---

## Setup — first-time connector configuration

Triggered by phrases like *"set up the Slack connector"*, *"connect mem-fusion to Slack"*, *"how do I share memories across my Macs"*.

### Procedure

1. **Check existing config.** Read `~/.local/share/mem-fusion/connector.json`. If a `slack`-type entry exists and the user isn't asking to add another one, report current state:

   ```
   Connector "<id>" already configured for channel "<channel>".
   Push: /remember <content> for <id>
   Pull: /remember pull <id>
   ```

2. **Probe Slack MCP availability.** Call `slack_search_channels(query="general")`. On error or no Slack MCP tools available: write an empty `{"connectors": []}` to `connector.json` and report *"Slack MCP not available in this Claude install — mem-fusion is set up local-only. Authorize the Slack MCP in Claude Code settings and re-run this skill."*

3. **Pick the channel.** Default suggestion for cross-machine personal sync: `mf-<userhandle>-devices` (private channel, just for the user's Macs). Allow the user to:
   - Use the suggestion (Claude offers to create it if absent).
   - Name an existing channel (e.g., `engineering`, `mf-test-connector`).
   - Skip and configure later.

4. **Resolve the channel id.** Use `slack_search_channels(query=<name>, channel_types="public_channel,private_channel")`. If multiple matches, present options. If zero matches and the user wants to create: explain that channel creation isn't in the Slack MCP surface — they'll need to create it manually in Slack, then re-run setup.

5. **Bot-user invite preflight.** Try `slack_read_channel(channel_id, limit=1)`. Two outcomes:
   - Returns messages OR `channel_not_found` with the user IS a member → push will work (the Slack MCP posts AS the user; see durable rule memory `21ff97b6`).
   - Returns `not_in_channel` → surface *"Invite required: open <channel> in Slack and run `/invite @claude_ai`."*

6. **Write `connector.json`.** Add the new entry; preserve existing entries:

   ```json
   {
     "connectors": [
       {"id": "<id>", "type": "slack", "channel": "<channel-id-or-name>"}
     ]
   }
   ```

   First-pull cap (optional, architect-acked at `docs/v0.5_ARCHITECT_ACK_2026-05-17.md` §B.1): add `"first_pull_max_age_days": N` if the channel has a long history the user only wants partial back-fill from.

7. **Smoke test.** Store + push + read-back:
   - `mem-fusion/store_memory({content: "<install-complete marker>", type: "context", tags: ["is_smoke_test"], connector_ids: ["<id>"]})`
   - `/remember push <id>` flow: `build_connector_envelope` → real `slack_send_message` → `set_connector_cursor`.
   - `slack_read_channel(channel_id, limit=1)` to verify the message landed.
   - `mem-fusion/ingest_connector_message(body=<msg.text>, connector_id=<id>)` to verify the receive path parses + integrity-checks. Expect `loopback_skipped` (since we sent it) or `smoke_test_skipped` (the G11 default filter — verify the marker is honored).

8. **Report result** with a one-line summary of what was tested + what's now possible.

---

## Push/pull operation — Slack-specific knowledge

Generic dispatch is `/remember push <id>` / `/remember pull <id>` per the `remember` skill. The Slack-specific bits this skill carries:

### Channel-id vs channel-name

`connector.json`'s `channel` field can be either a Slack channel id (`C0XXXXXXX`) or a name (`mem_fusion_engineering`). At push time:
- If it matches `^C[A-Z0-9]{9,}$`: pass directly to `slack_send_message(channel_id=…)`.
- Otherwise: resolve via `slack_search_channels(query=<name>)`. If multiple/zero matches, abort the push with the search results so the user can update `connector.json` with the resolved id.

### Body size cap

§5.4 of `docs/v0.5_CONNECTOR_ARCHITECTURE.md` documents a 38 KB push-side cap (40 KB Slack ceiling). `build_connector_envelope` enforces it and returns `body_too_large_for_substrate` on exceedance. Cause: the embedded 768-dim vector (~8 KB JSON) plus large content. Mitigation: omit the vector from the wire envelope (receiver re-embeds via Ollama at ~25 ms cost).

### Slack fence normalization

Slack ingests fenced code blocks and normalizes:
- Strips the `json` language hint after the opening fence.
- Strips the inside-fence newlines.

JSON content between fences is preserved byte-exact. Our parser handles both shapes (string-find + `json.loads`); this is in §5.1 of the architecture doc, verified by E2E run on 2026-05-18.

### Auth tier — user vs bot

The current Slack MCP posts AS the user (user-tier OAuth, e.g., `UKN4XM7T3`). In that setup:
- Channel-membership covers both send and receive (the user IS the sender).
- The bot-invite preflight described in setup §5 is a conservative path for future bot-tier connectors (Slack app with bot token, GDrive service account, etc.).
- See durable rule memory `21ff97b6` for the empirical verification.

### Rate-limit awareness

`slack_send_message` and `slack_read_channel` share the Slack app's per-app rate-limit budget. v0.5 push/pull orchestration is sequential (architect-acked in `docs/v0.5_ARCHITECT_ACK_2026-05-17.md` §B.4) so parallel sends never share the budget within one push. If a rate-limit error surfaces during push, the cursor stays where it was (only advances on per-message success) — the user re-runs `/remember push <id>` after the rate-limit window passes; already-delivered messages dedup as duplicates on re-attempt.

---

## Multi-connector management

### Adding another channel

A memory can be tagged with multiple `connector_ids`. To set up an additional Slack channel:

1. Run this skill (same procedure as first-time setup); step 1 detects the existing entry and offers "add a new connector" path instead of reporting existing.
2. The new entry appends to `connector.json`'s `connectors` array.
3. Existing memories aren't auto-tagged with the new id. Use `mem-fusion/add_connector_ids(memory_ids=[…], connector_ids=["<new-id>"])` to retroactively widen specific memories.

### Changing a channel target

The `channel` field is editable by hand. Save the change to `connector.json`. Note: the connector's cursor (`~/.local/share/mem-fusion/connector_cursors.json` keyed by `<id>`) is keyed by connector id, not channel — changing the channel target keeps the cursor, so next pull starts from the OLD cursor's timestamp against the NEW channel. Treat as a new connector if that's not what you want: delete the cursor entry before pulling.

### Additive-only constraint

`connector_ids` is additive only — `add_connector_ids` widens, never removes. Documented limitation in v0.5; soft-mute via empty channel is the workaround if a memory is mistakenly tagged. v0.6 will design a proper remove path (architect-acked, deferred per `docs/v0.5_ARCHITECT_ACK_2026-05-17.md` §D G4).

---

## Troubleshooting

| Symptom | Likely cause | Remediation |
|---|---|---|
| `slack_send_message` returns auth error | Slack MCP OAuth expired | Re-authenticate the Slack MCP in Claude Code settings, then re-run |
| `channel_not_found` | Channel renamed, deleted, or wrong workspace | Verify channel exists in Slack; update `connector.json`'s `channel` field |
| `not_in_channel` | Bot user not invited (bot-tier MCP only) | Open the channel in Slack, `/invite @<bot>` |
| Push succeeds but pull returns 0 stored | Receiver hasn't deployed canonical core.py | On the receiver: `bash src/scripts/fixes/0.5.0-015-*.sh` |
| `integrity_failed` on receive | content_hash drift (pre-015 sender + post-015 receiver) | Both peers run `0.5.0-015` fix script |
| Rate-limit sustained | Slack app over per-app budget | Wait the indicated window; re-run push — cursor handles resume |
| Body too large (>38 KB) | Memory + 768-dim vector exceeds cap | Re-store without the vector OR ship as a `gist`/Drive link rather than full content |
| Push works locally but architect can't see it | Architect's daemon not running | Architect machine must have constellation running + run `0.5.0-018` for cross-version hash tolerance |
| `smoke_test_skipped` on a non-test memory | `is_smoke_test: true` snuck onto the envelope | Re-store without that tag; pass `include_smoke_tests=true` if you want the existing message to land |

If a failure isn't on the table: search `mem-fusion` for the symptom (`search_memory(query="<symptom>", type="error")`) — this peer's history may already have a recipe.

---

## Anti-patterns

- **Don't manually edit `~/.local/share/mem-fusion/connector_cursors.json`** during a push or pull — atomic-write semantics are managed by `set_connector_cursor`.
- **Don't post directly to a connector channel from this skill** — orchestrate through `build_connector_envelope` so the envelope's wire-format integrity stays canonical. Hand-composed Slack posts are a debugging tool, not a production path.
- **Don't share `connector.json` across machines via dotfile sync**. Channel-ids are workspace-specific; sharing risks pointing one machine's connector at another machine's channel-id by accident. Each machine declares its own.

---

## Related artifacts

- `docs/v0.5_CONNECTOR_ARCHITECTURE.md` — wire format §5.1, full dispatch contract §6
- `docs/v0.5_ARCHITECT_ACK_2026-05-17.md` — architect decisions on first-pull semantics, G3/G5/G11
- `~/.claude/skills/remember/SKILL.md` — generic dispatch for `/remember push|pull <name>`
- Memory `1062a5ed` — Stage 1 consolidation pointer (commits, MCP tools, tests, E2E results)
- Memory `21ff97b6` — bot-invite preflight clarification (user-tier vs bot-tier)
- Memory `d0efaea7` — G11 filter ordering durable rule
