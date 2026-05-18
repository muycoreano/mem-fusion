#!/usr/bin/env python3
"""
v0.5 Slack connector e2e round-trip test (parse + receive path).

Exercises _SLACK.parse_envelope + core.store_memory_from_envelope
against synthetic Slack-normalized message bodies that match what
slack_read_channel would return after Slack's fence normalization
(`json` language hint stripped, inside-fence newlines stripped).

Six cases:
  1. Peer envelope, fresh content       → stored
  2. Same peer envelope re-ingested     → duplicate
  3. Same peer, additional connector_id → merged (additive widening)
  4. Own envelope (origin == self)      → loopback_skipped
  5. Tampered envelope (hash mismatch)  → integrity_failed
  6. Envelope missing required field    → missing_field error
  7. Envelope with valid 768-dim vector → stored, vector preserved
                                          (no re-embedding via Ollama)

Runs against the LOCAL running Qdrant; uses a unique project tag for
isolation and cleans up created memories before exit. Exit code 0 on
all-pass, 1 on failure.

Usage:
  ~/.local/share/mem-fusion/venv/bin/python tests/mem_fusion/test-connector-e2e-roundtrip.py
"""
import asyncio
import hashlib
import json
import pathlib
import sys
import time
import uuid

# Module-level core import so make_body can call format_envelope_for_slack
# without threading core through every call site.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))
import core                          # noqa: E402
from connectors import get_connector # noqa: E402

#: Module-level Slack connector instance — single source of truth for the
#: wire format. Tests reach for it via get_connector("slack") so they
#: also exercise the registry path.
_SLACK = get_connector("slack")


def make_body(envelope: dict, simulate_slack_normalization: bool = True) -> str:
    """Test helper that constructs a Slack message body matching §5.1.

    Delegates to `SlackConnector.format_envelope` via the connector
    registry — single source of truth for the Slack wire format.
    Exercises BOTH forms:
      - simulate_slack_normalization=True (default): the body shape that
        slack_read_channel returns AFTER Slack's fence normalization
        (strips `json` language hint, strips inside-fence newlines).
        Used by every test case below.
      - simulate_slack_normalization=False: the raw sender-side form, as
        returned by `_SLACK.format_envelope` directly.
    Both forms must parse cleanly via `_SLACK.parse_envelope`.
    """
    raw = _SLACK.format_envelope(envelope)
    if not simulate_slack_normalization:
        return raw
    # Simulate Slack's normalization: strip `json` hint + inside-fence newlines
    return raw.replace("```json\n", "```").replace("\n```", "```")


def main():

    project_tag = f"test-connector-e2e-{int(time.time())}"
    created_ids: list[str] = []
    failures: list[str] = []

    def check(name: str, cond: bool, msg: str = ""):
        prefix = "  ✓" if cond else "  ✗"
        print(f"{prefix} {name}{(' — ' + msg) if msg else ''}")
        if not cond:
            failures.append(name)

    def wire_hash(text: str) -> str:
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    self_node = core._resolve_node_name()
    print(f"SELF_NODE: {self_node}\n")

    # ───────────────────────────────────────────────────────────────────
    # Case 1 — peer envelope, fresh content → stored
    # ───────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("Case 1: peer envelope, fresh content → stored")
    print("=" * 60)
    content1 = f"e2e test case 1 from peer-x: fresh content {uuid.uuid4()}"
    env1 = {
        "envelope_version": 1,
        "content_hash":     wire_hash(content1),
        "origin_node":      "fake-peer-x",
        "submitted_at":     "2026-05-17T10:00:00+00:00",
        "content":          content1,
        "type":             "context",
        "tags":             ["e2e-test"],
        "project":          project_tag,
        "importance":       3,
        "connector_ids":    ["test"],
    }
    body1 = make_body(env1)
    parsed1 = _SLACK.parse_envelope(body1)
    check("parse_slack_envelope returns dict",
          isinstance(parsed1, dict) and parsed1.get("content") == content1)
    r1 = asyncio.run(core.store_memory_from_envelope(parsed1))
    check("store_memory_from_envelope on fresh peer envelope → stored",
          r1.get("status") == "stored", f"got: {r1}")
    if r1.get("id"):
        created_ids.append(r1["id"])

    # ───────────────────────────────────────────────────────────────────
    # Case 2 — same envelope re-ingested → duplicate
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 2: same envelope re-ingested → duplicate")
    print("=" * 60)
    parsed2 = _SLACK.parse_envelope(body1)  # same body
    r2 = asyncio.run(core.store_memory_from_envelope(parsed2))
    check("re-ingest same content → duplicate",
          r2.get("status") == "duplicate", f"got: {r2}")
    check("duplicate.id matches first store",
          r2.get("id") == r1.get("id"), f"got: {r2.get('id')}, expected {r1.get('id')}")

    # ───────────────────────────────────────────────────────────────────
    # Case 3 — same content, additional connector_id → merged
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 3: same content, additional connector_id → merged (additive)")
    print("=" * 60)
    env3 = dict(env1)
    env3["connector_ids"] = ["test", "test-channel-2"]
    parsed3 = _SLACK.parse_envelope(make_body(env3))
    r3 = asyncio.run(core.store_memory_from_envelope(parsed3))
    check("same content + new connector_id → merged",
          r3.get("status") == "merged" and
          set(r3.get("connector_ids", [])) == {"test", "test-channel-2"},
          f"got: {r3}")

    # ───────────────────────────────────────────────────────────────────
    # Case 4 — own envelope (origin == self) → loopback_skipped
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 4: own envelope → loopback_skipped")
    print("=" * 60)
    content4 = f"e2e test case 4 from self: should be loopback-skipped {uuid.uuid4()}"
    env4 = dict(env1)
    env4["origin_node"] = self_node
    env4["content"] = content4
    env4["content_hash"] = wire_hash(content4)
    parsed4 = _SLACK.parse_envelope(make_body(env4))
    r4 = asyncio.run(core.store_memory_from_envelope(parsed4))
    check("envelope from self → loopback_skipped",
          r4.get("status") == "loopback_skipped", f"got: {r4}")
    # Verify nothing was stored
    sr = asyncio.run(core.search_memory({
        "query": content4, "top_k": 5, "project": project_tag,
    }))
    matches = sr.get("results", [])
    own_match = [r for r in matches if r.get("content") == content4]
    check("loopback-skipped content NOT in local store",
          len(own_match) == 0, f"unexpected hits: {len(own_match)}")

    # ───────────────────────────────────────────────────────────────────
    # Case 5 — tampered envelope (hash mismatch) → integrity_failed
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 5: tampered envelope (content modified after hash) → integrity_failed")
    print("=" * 60)
    content5 = f"e2e test case 5 tampered {uuid.uuid4()}"
    env5 = dict(env1)
    env5["content"] = content5
    env5["content_hash"] = wire_hash(content5)  # legit hash
    # Now tamper: change content, leave hash
    env5["content"] = content5 + " <tampered>"
    parsed5 = _SLACK.parse_envelope(make_body(env5))
    r5 = asyncio.run(core.store_memory_from_envelope(parsed5))
    check("tampered envelope → integrity_failed",
          r5.get("error") == "integrity_failed", f"got: {r5}")
    check("integrity_failed includes expected + received hashes",
          "expected" in r5 and "received" in r5,
          f"keys: {list(r5.keys())}")

    # ───────────────────────────────────────────────────────────────────
    # Case 6 — missing required field → missing_field error
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 6: envelope missing required field → missing_field error")
    print("=" * 60)
    env6 = dict(env1)
    env6.pop("origin_node")
    parsed6 = _SLACK.parse_envelope(make_body(env6))
    r6 = asyncio.run(core.store_memory_from_envelope(parsed6))
    check("missing origin_node → missing_field error",
          r6.get("error") == "missing_field", f"got: {r6}")

    # ───────────────────────────────────────────────────────────────────
    # Case 7 — envelope with valid 768-dim vector → stored, vector preserved
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 7: envelope with valid 768-dim vector → stored without re-embedding")
    print("=" * 60)
    content7 = f"e2e test case 7 with embedded vector {uuid.uuid4()}"
    sent_vec = [float(i % 7) / 10.0 for i in range(768)]  # deterministic but bogus
    env7 = dict(env1)
    env7["content"] = content7
    env7["content_hash"] = wire_hash(content7)
    env7["vector"] = sent_vec
    parsed7 = _SLACK.parse_envelope(make_body(env7))
    r7 = asyncio.run(core.store_memory_from_envelope(parsed7))
    check("envelope with valid vector → stored",
          r7.get("status") == "stored", f"got: {r7}")
    if r7.get("id"):
        created_ids.append(r7["id"])
    # Verify the stored vector matches the L2-normalized sent vector.
    # Qdrant normalizes vectors for Cosine collections on store; the test
    # for "vector path used, no re-embedding" is that the stored vector
    # is the unit-norm of the sent vector. Re-embedding (the other code
    # path) would produce a completely different vector that wouldn't
    # match the normalized sent.
    if r7.get("id"):
        import math
        pts = core.qdrant.retrieve(collection_name=core.COLLECTION,
                                   ids=[r7["id"]], with_vectors=True)
        if pts:
            stored_vec = list(pts[0].vector or [])
            norm = math.sqrt(sum(x * x for x in sent_vec))
            sent_unit = [x / norm for x in sent_vec] if norm > 0 else sent_vec
            vec_matches = (len(stored_vec) == 768 and
                          all(abs(a - b) < 1e-5 for a, b in zip(stored_vec, sent_unit)))
            check("stored vector == L2-normalize(sent_vec) → vector path used, no re-embed",
                  vec_matches,
                  f"len={len(stored_vec)}, match={'yes' if vec_matches else 'no'}")

    # ───────────────────────────────────────────────────────────────────
    # Cleanup
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Cleanup")
    print("=" * 60)
    if created_ids:
        core.qdrant.delete(collection_name=core.COLLECTION,
                           points_selector=[i for i in created_ids if i])
        print(f"  ✓ deleted {len(created_ids)} test memories")

    print()
    print("=" * 60)
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        sys.exit(1)
    print("ALL E2E ROUND-TRIP TESTS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
