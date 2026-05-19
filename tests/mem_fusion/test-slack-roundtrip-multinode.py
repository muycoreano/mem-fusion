#!/usr/bin/env python3
"""
v0.5 Slack connector multi-node round-trip — wire-format + receive-flow.

Scope (per `docs/v0.5_CONNECTOR_ARCHITECTURE.md` §6):

  This is a DAEMON-SIDE test. It exercises the full sender + receiver
  chain MINUS the Slack-MCP transport (`slack_send_message` /
  `slack_read_channel`), which is invoked from a Claude session and is
  out of scope for the Python test suite — per the same separation
  documented in test-connector-push-orchestration.py:12 and
  test-connector-pull-orchestration.py:11.

  Slack's fence normalization is simulated in-process via
  slack_normalize() (same helper test-connector-e2e-roundtrip.py uses)
  so the parser is exercised against the harder of the two on-the-wire
  shapes.

Exercises (per interaction):

  core.store_memory  →  core.export_record
    → SlackConnector.build_envelope_from_record  (production sender helper)
    → SlackConnector.format_envelope             (wire serialization)
    → slack_normalize                            (models slack_read_channel)
    → SlackConnector.parse_envelope              (receive-side extraction)
    → SlackConnector.verify_integrity            (wire-hash check)
    → core.ingest_connector_message              (full receive flow + G11)

Scenarios — 3 scenarios × 10 interactions × 3 rotating simulated nodes:

  1. Engineers bug-fix flow            (10 interactions, 3 nodes)
  2. Product managers strategy thread  (10 interactions, 3 nodes)
  3. Executives business-strategy      (10 interactions, 3 nodes)

about-me attribution is encoded via origin_node + an envelope extension
field `about_me` (forward-tests the v0.6 schema; the Envelope TypedDict
is total=False so extension fields round-trip unchanged).

Pass criteria per interaction:
  - parse_envelope returns a dict
  - verify_integrity returns ok
  - envelope.origin_node matches the simulated node for that interaction
  - envelope.about_me matches the node's about_me string byte-exact
  - envelope.content matches the interaction text byte-exact
  - ingest reports stored/duplicate/merged (G11 override via
    include_smoke_tests=True since envelopes carry is_smoke_test=true)

Live MCP-driven validation against #mf-test-connector is a separate
Claude-session activity: build_connector_envelope (mem-fusion MCP) →
slack_send_message (Slack MCP) → slack_read_channel → ingest_connector_message.

Cleanup: deletes every memory created during the test.
"""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import uuid

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import core                                  # noqa: E402
from connectors import get_connector         # noqa: E402

_SLACK = get_connector("slack")

# ── Test-run identity ──────────────────────────────────────────────────────
SESSION_TAG   = f"roundtrip-{uuid.uuid4().hex[:8]}"
PROJECT_TAG   = "mf-slack-roundtrip-test"

#: Slack-channel id reference (same as the other engineer-written tests
#: under tests/mem_fusion/). Not used for transport here — this test
#: never speaks to Slack — but recorded so the connector-config the test
#: stands up matches the production-shape used by every other test.
TEST_CHANNEL_ID = "C0B3VGB3RF1"


def slack_normalize(raw_body: str) -> str:
    """Same fence-normalization as test-connector-e2e-roundtrip.py."""
    return raw_body.replace("```json\n", "```").replace("\n```", "```")


# ── Failure tracking ──────────────────────────────────────────────────────
_FAILURES: list[str] = []
_CREATED_IDS: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name} — {msg}")
        _FAILURES.append(name)


# ── Scenarios ─────────────────────────────────────────────────────────────
#
# Each scenario defines 3 simulated nodes (with about-me strings) and 10
# realistic interactions reflecting that persona's working voice. Interactions
# rotate through nodes (node[i % 3]). Content varies enough that no two
# interactions content_hash-collide.

SCENARIOS = {
    "engineers_bugfix": {
        "type":    "decision",
        "project": "v0.5-bug-fix-thread",
        "nodes": [
            {"node_name": "engineer-1",
             "about_me":  "Claude-Engineer on engineer-1 — owns storage layer "
                          "(Qdrant collection, payload schema, embedding pipeline). "
                          "Closes content_hash + chunked-embedding issues."},
            {"node_name": "engineer-2",
             "about_me":  "Claude-Engineer on engineer-2 — owns connector substrate "
                          "(Slack wire format, integrity, push/pull orchestration). "
                          "Closes envelope-parse and rate-limit issues."},
            {"node_name": "engineer-3",
             "about_me":  "Claude-Engineer on engineer-3 — owns hooks + skill "
                          "orchestration (SessionStart, UserPromptSubmit, Stop). "
                          "Closes session-priming and capture bugs."},
        ],
        "interactions": [
            "Reproduced: large memories returning embed_failed despite EMBED_MAX_CHARS guard.",
            "Root cause: nomic-embed-text's 2048-token context exceeded at 8000 chars on dense markdown.",
            "Proposed fix: chunked embedding with canonical_id; chunks share metadata.",
            "Architect ACK'd chunking direction; aligned with v0.6 substrate bump trajectory.",
            "Implemented chunk_text() helper with 200-char overlap window.",
            "Wrote embed_chunks() helper using Ollama's /api/embed batch endpoint.",
            "Rewrote search_memory: over-fetch top_k*3 + dedupe by canonical_id at search time.",
            "Updated delete_memory to cascade across all chunks via canonical_id.",
            "Test suite: 22 chunked-embedding cases green, 4 pre-existing suites still pass.",
            "Shipped as 0.5.0-019; Marketing bug 581c761d closed; full-path QA green.",
        ],
    },
    "pm_strategy": {
        "type":    "context",
        "project": "v0.7-product-strategy",
        "nodes": [
            {"node_name": "pm-1",
             "about_me":  "Claude-Product-Manager on pm-1 — owns positioning + audience "
                          "definition. Tracks competitive landscape against Mem0, gbrain, "
                          "Letta. Focused on Claude Code dev audience."},
            {"node_name": "pm-2",
             "about_me":  "Claude-Product-Manager on pm-2 — owns metrics + measurement "
                          "plan. Defines the four axes of best (retrieval, latency, "
                          "robustness, trust) and the proof stack."},
            {"node_name": "pm-3",
             "about_me":  "Claude-Product-Manager on pm-3 — owns adoption telemetry "
                          "(opt-in mf metrics CLI) and lifecycle surveys. Bridges product "
                          "to growth."},
        ],
        "interactions": [
            "Anthropic shipped memory_20250818 — primitive, not product. Application backend left unfilled. Market expands ~10x.",
            "Reframing v0.7 from 'memory layer for Claude Code' to 'memory backend for Anthropic SDK ecosystem.'",
            "Four axes of best (retrieval, latency, robustness, trust) — today we win axis 4, competitive on 2, weak on 1+3.",
            "WP-1 benchmarks: LOCOMO + LongMemEval + multi-machine coordination bench. Methodology must be transparent.",
            "Audience laddering: Claude Code power devs first; privacy-conscious teams next; agent-framework integrators after.",
            "mf metrics CLI proposal: zero passive collection; user opt-in voluntary share of anonymized counts + latencies.",
            "Adoption funnel: top = GitHub stars; mid = install completions; bottom = external PRs + community connectors.",
            "Quality-over-time proof: monthly retrospective on memory junk rate; corrective-memory pattern as the architectural answer.",
            "Lifecycle surveys at L+30 / L+90 / L+180 days. Calibration proposals, not commitments until target review.",
            "Six open questions queued for architect+marketing review before v0.7 targets bind.",
        ],
    },
    "execs_strategy": {
        "type":    "decision",
        "project": "v0.7-launch-business-strategy",
        "nodes": [
            {"node_name": "exec-1",
             "about_me":  "Claude-Executive on exec-1 — capital + business model lead. "
                          "Frames mem-fusion's OSS-first posture against revenue paths. "
                          "Owns founder-positioning for category narrative."},
            {"node_name": "exec-2",
             "about_me":  "Claude-Executive on exec-2 — go-to-market lead. Owns "
                          "developer relations, plugin marketplace distribution, "
                          "partnerships (OpenClaw, Anthropic SDK ecosystem)."},
            {"node_name": "exec-3",
             "about_me":  "Claude-Executive on exec-3 — operations + delivery lead. "
                          "Tracks engineering throughput, install-flow polish, "
                          "post-launch incident posture. Bridges product to ship."},
        ],
        "interactions": [
            "v0.5 ship gate clears: install.sh exit 0, 10/10 tests, daemons healthy. Ready to tag.",
            "Strategic frame: we serve the segment Anthropic deliberately left open (client-controlled memory backend). Complementary to Managed Agents, not competitive.",
            "Category positioning: 'memory mesh + identity routing + consent-keyed sharing' — local-first is now table stakes.",
            "Distribution channel: Claude Code plugin marketplace as primary; PyPI package for SDK ecosystem.",
            "Partnership posture: OpenClaw as distribution channel, not competitor. Anthropic as protocol partner, not vendor.",
            "OSS model: MIT or Apache-2.0; lean Apache for patent grant. Decision before tagging v1.0.",
            "Capital lens: every v0.7 WP either closes a credibility gap (benchmarks) or extends a unique lead (work-queue). No vanity features.",
            "Risk register: Mem0 community size (47K stars), Anthropic shipping default backend, gbrain feature catch-up. Top of stack.",
            "Founder narrative: 'they shipped the surface, we ship the substance.' Mutually elevating, never adversarial.",
            "Next gates: v0.5 tag this week, v0.6 about_me identity Q3, v0.7 public launch + benchmarks Q4.",
        ],
    },
}


# ── Round-trip ─────────────────────────────────────────────────────────────
async def _build_local_record(content: str, persona_type: str, project: str) -> dict:
    """Store + export a record locally. Returns the export shape."""
    sr = await core.store_memory({
        "content":    content,
        "type":       persona_type,
        "tags":       ["roundtrip-test", SESSION_TAG],
        "project":    project,
        "importance": 3,
    })
    if sr.get("status") not in ("stored", "merged", "duplicate"):
        raise RuntimeError(f"store_memory failed: {sr}")
    _CREATED_IDS.append(sr["id"])
    return await core.export_record({"id": sr["id"]})


def _as_node_record(record: dict, node_name: str, about_me: str) -> dict:
    """Swap origin_node + attach about_me. Single-machine simulation of cross-peer arrival."""
    out = dict(record)
    out["origin_node"] = node_name
    out["about_me"]    = about_me
    return out


async def run_scenario(scenario_key: str, scenario: dict) -> None:
    print()
    print("=" * 70)
    print(f"Scenario: {scenario_key}")
    print(f"  type     = {scenario['type']}")
    print(f"  project  = {scenario['project']}")
    print(f"  nodes    = {[n['node_name'] for n in scenario['nodes']]}")
    print(f"  count    = {len(scenario['interactions'])} interactions")
    print("=" * 70)

    nodes = scenario["nodes"]
    interactions = scenario["interactions"]

    for i, content in enumerate(interactions):
        node = nodes[i % len(nodes)]
        node_name = node["node_name"]
        about_me  = node["about_me"]

        # Build a local record, then swap origin_node + attach about_me
        # so it looks like a record from the simulated node.
        rec_local = await _build_local_record(
            content, scenario["type"], scenario["project"],
        )
        rec_remote = _as_node_record(rec_local, node_name, about_me)

        # Delete the local source — simulate true cross-peer arrival.
        core.qdrant.delete(collection_name=core.COLLECTION,
                           points_selector=[rec_local["id"]])
        try:
            _CREATED_IDS.remove(rec_local["id"])
        except ValueError:
            pass

        # Build envelope via the production helper.
        env = _SLACK.build_envelope_from_record(rec_remote, "test-roundtrip")

        # Mark as smoke-test so production peers' G11 filter would skip.
        env["is_smoke_test"] = True
        # about_me is an envelope extension field (v0.6 schema preview).
        env["about_me"] = about_me

        body = _SLACK.format_envelope(env)
        # Model what slack_read_channel would return: Slack-normalized fences.
        body_recovered = slack_normalize(body)

        # Parse the body Slack returned (after fence normalization).
        parsed = _SLACK.parse_envelope(body_recovered)
        check(f"[{scenario_key} #{i+1}/{node_name}] parse_envelope → dict",
              isinstance(parsed, dict) and parsed.get("envelope_version") == 1,
              f"got {type(parsed).__name__}")

        if not isinstance(parsed, dict):
            continue

        ok, detail = _SLACK.verify_integrity(parsed)
        check(f"[{scenario_key} #{i+1}/{node_name}] verify_integrity → ok",
              ok is True, f"detail: {detail}")

        check(f"[{scenario_key} #{i+1}/{node_name}] origin_node preserved",
              parsed.get("origin_node") == node_name,
              f"got {parsed.get('origin_node')}")
        check(f"[{scenario_key} #{i+1}/{node_name}] about_me preserved (v0.6 preview)",
              parsed.get("about_me") == about_me,
              f"got {parsed.get('about_me')!r}")
        check(f"[{scenario_key} #{i+1}/{node_name}] content preserved",
              parsed.get("content") == content,
              f"got {parsed.get('content')!r}")

        # Ingest via the receive flow. Override G11 since we want the
        # round-trip to actually store the memory.
        result = await core.ingest_connector_message({
            "body":               body_recovered,
            "connector_id":       "test-roundtrip",
            "include_smoke_tests": True,
        })
        status = result.get("status") or result.get("error")
        check(f"[{scenario_key} #{i+1}/{node_name}] ingest → stored/duplicate/merged",
              status in ("stored", "duplicate", "merged"),
              f"got status={status}, result={result}")

        if result.get("id"):
            _CREATED_IDS.append(result["id"])


def main() -> None:
    print()
    print("█" * 70)
    print("  Slack connector round-trip — multi-node, multi-scenario")
    print("  Daemon-side wire + receive-flow (transport simulated via slack_normalize)")
    print(f"  Session tag: {SESSION_TAG}")
    print("█" * 70)

    # Stand up a temp connector.json with a `test-roundtrip` slack entry
    # so ingest_connector_message can resolve the connector_id without
    # touching the user's real config. Pattern matches
    # tests/mem_fusion/test-connector-pull-orchestration.py:52.
    original_cfg_path = core.CONNECTORS_CONFIG_PATH
    fd, cfg_path = tempfile.mkstemp(suffix=".json", prefix="mf-roundtrip-")
    with os.fdopen(fd, "w") as f:
        json.dump({"connectors": [
            {"id": "test-roundtrip", "type": "slack", "channel": TEST_CHANNEL_ID},
        ]}, f)
    core.CONNECTORS_CONFIG_PATH = pathlib.Path(cfg_path)

    try:
        for key, scenario in SCENARIOS.items():
            asyncio.run(run_scenario(key, scenario))
    finally:
        # Restore the user's real connectors config so subsequent processes
        # in this interpreter aren't poisoned by the test override.
        core.CONNECTORS_CONFIG_PATH = original_cfg_path
        try:
            os.unlink(cfg_path)
        except OSError:
            pass

    # ── Cleanup ──────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("Cleanup")
    print("=" * 70)
    # Dedupe + filter falsy
    ids = list({i for i in _CREATED_IDS if i})
    if ids:
        core.qdrant.delete(collection_name=core.COLLECTION, points_selector=ids)
        print(f"  ✓ deleted {len(ids)} test memories")
    else:
        print("  (no memories to delete)")

    # ── Verdict ──────────────────────────────────────────────────────
    print()
    print("=" * 70)
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)} checks): {', '.join(_FAILURES[:5])}"
              + (' ...' if len(_FAILURES) > 5 else ''))
        sys.exit(1)
    print("ALL SLACK ROUND-TRIP TESTS PASSED "
          "(30 interactions × 3 scenarios × multi-node × about-me attribution)")
    sys.exit(0)


if __name__ == "__main__":
    main()
