#!/usr/bin/env python3
"""
v0.5 Slack connector — LIVE MCP-driven push+pull roundtrip test.

Two-phase architecture (per docs/v0.5_CONNECTOR_ARCHITECTURE.md §6):

  Phase A — this script (Python on the daemon side):
    Stores two local memories. Builds two canonical Slack envelope bodies:
      body_self : origin_node = this peer  (loopback case)
      body_fake : origin_node = fake-peer  (cross-peer case)
    Cross-peer arrival is simulated by deleting the local source of the
    fake-peer memory BEFORE export, so receive-side dedup doesn't fold it
    into the original record. Emits a single JSON object with both bodies,
    their ids, and the fake-peer name.

  Phase B — Claude session (the only place the Slack MCP exists):
    Posts body_self  via slack_send_message       → message lands in channel
    Posts body_fake  via slack_send_message       → message lands in channel
    Reads both back  via slack_read_channel        → Slack-normalized bodies
    Ingests body_self via ingest_connector_message → expects loopback_skipped
    Ingests body_fake via ingest_connector_message → expects stored
    Cleans up: deletes id_self locally, deletes the new fake-peer ingest id.

Pass criteria (verified in Phase B):
  - body_self ingest → status = loopback_skipped, detail mentions this peer
  - body_fake ingest → status = stored, returned id distinct from id_self
  - exporting the new id yields origin_node = the fake-peer name we minted
  - Slack message bodies parse cleanly through SlackConnector.parse_envelope
    after Slack's server-side fence normalization

Why two phases: the daemon has no Slack credentials (per architecture);
the Slack MCP lives in Claude. This mirrors the explicit non-scope of
test-connector-push-orchestration.py:12 and test-connector-pull-orchestration.py:11.

Usage:
  Phase A: ~/.local/share/mem-fusion/venv/bin/python \\
             tests/mem_fusion/test-slack-mcp-live-roundtrip.py
           → emits {body_self, body_fake, id_self, fake_peer, session_tag}

  Phase B: driven by Claude using mcp__claude_ai_Slack__slack_send_message,
           slack_read_channel, mcp__mem-fusion__ingest_connector_message,
           and mcp__mem-fusion__delete_memory.
"""
import asyncio
import json
import pathlib
import sys
import uuid

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import core                                  # noqa: E402
from connectors import get_connector         # noqa: E402

_SLACK = get_connector("slack")

SESSION_TAG  = f"live-{uuid.uuid4().hex[:8]}"
CONNECTOR_ID = "test-roundtrip"
PROJECT      = "mf-slack-mcp-live-roundtrip"


async def build_pair() -> dict:
    # ── Case A: self-origin → expects loopback_skipped on receive ─────────
    sr1 = await core.store_memory({
        "content": (
            f"[SMOKE TEST {SESSION_TAG}] Case A — self-origin envelope. "
            "Receive flow should detect origin == this peer and return "
            "loopback_skipped (no store on the local side; integrity passed)."
        ),
        "type": "context",
        "tags": ["live-mcp-roundtrip", "case-self-origin", SESSION_TAG],
        "project": PROJECT,
        "importance": 1,
        "connector_ids": [CONNECTOR_ID],
    })
    if sr1.get("status") not in ("stored", "merged", "duplicate"):
        raise RuntimeError(f"store_memory case-A failed: {sr1}")
    rec1 = await core.export_record({"id": sr1["id"]})
    env1 = _SLACK.build_envelope_from_record(rec1, CONNECTOR_ID)
    env1.pop("vector", None)  # smaller body; receiver re-embeds via Ollama
    body_self = _SLACK.format_envelope(env1)

    # ── Case B: fake-peer origin → expects stored on receive ─────────────
    fake_peer = f"fake-peer-{uuid.uuid4().hex[:8]}"
    sr2 = await core.store_memory({
        "content": (
            f"[SMOKE TEST {SESSION_TAG}] Case B — fake-peer origin "
            f"'{fake_peer}'. Receive flow should ingest (origin != this "
            "peer, wire integrity passes, content_hash dedup decides "
            "stored vs duplicate vs merged)."
        ),
        "type": "context",
        "tags": ["live-mcp-roundtrip", "case-fake-peer", SESSION_TAG],
        "project": PROJECT,
        "importance": 1,
        "connector_ids": [CONNECTOR_ID],
    })
    if sr2.get("status") not in ("stored", "merged", "duplicate"):
        raise RuntimeError(f"store_memory case-B failed: {sr2}")
    rec2 = await core.export_record({"id": sr2["id"]})
    rec2["origin_node"] = fake_peer  # only allowed hand-edit; simulates cross-peer arrival
    env2 = _SLACK.build_envelope_from_record(rec2, CONNECTOR_ID)
    env2.pop("vector", None)
    body_fake = _SLACK.format_envelope(env2)

    # Delete the local source of case B so receive truly stores (cross-peer arrival sim).
    core.qdrant.delete(collection_name=core.COLLECTION, points_selector=[sr2["id"]])

    return {
        "session_tag": SESSION_TAG,
        "fake_peer":   fake_peer,
        "id_self":     sr1["id"],
        "id_src_fake": sr2["id"],  # already deleted; recorded for trace
        "body_self":   body_self,
        "body_fake":   body_fake,
    }


def main() -> None:
    print(json.dumps(asyncio.run(build_pair()), indent=2))


if __name__ == "__main__":
    main()
