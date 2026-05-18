#!/usr/bin/env python3
"""
0.5.0-017 — /remember pull <connector-id> orchestration primitives test.

Exercises the new MCP primitive that powers pull orchestration:

  - ingest_connector_message(body, connector_id, include_smoke_tests?)
    composes Connector.parse_envelope + G11 smoke-test filter +
    core.store_memory_from_envelope.

NOT covered here: the Slack-side `slack_read_channel` call. That happens
in Claude's reasoning inside the skill, against the Slack MCP layer
which mem-fusion has no access to (per architecture doc §6).

Production helpers throughout; the only confined hand-edit is the
cross-peer arrival simulator (`origin_node` swap to a fake peer) used
across all e2e tests on a single test machine.

Runs against the LOCAL Qdrant.

Usage:
  ~/.local/share/mem-fusion/venv/bin/python tests/mem_fusion/test-connector-pull-orchestration.py
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

import core                            # noqa: E402
from connectors import get_connector   # noqa: E402

_SLACK = get_connector("slack")


def main():
    failures: list[str] = []
    cleanup_ids: list[str] = []
    cleanup_paths: list[str] = []

    def check(name: str, cond: bool, detail: str = ""):
        prefix = "  ✓" if cond else "  ✗"
        print(f"{prefix} {name}{(' — ' + detail) if detail else ''}")
        if not cond:
            failures.append(name)

    # Stand up a connector.json
    fd, cfg_path = tempfile.mkstemp(suffix=".json")
    cleanup_paths.append(cfg_path)
    with os.fdopen(fd, "w") as f:
        json.dump({"connectors": [
            {"id": "test", "type": "slack", "channel": "C0B3VGB3RF1"},
        ]}, f)
    core.CONNECTORS_CONFIG_PATH = pathlib.Path(cfg_path)

    fake_peer = f"fake-peer-{uuid.uuid4().hex[:8]}"

    async def make_real_envelope_body(content: str, extra_envelope_fields: dict | None = None) -> str:
        """Produce a real envelope body via the full sender chain.

        Production helpers: store_memory → export_record → build_envelope_from_record →
        format_envelope. Source memory deleted so receive truly stores.
        """
        sr = await core.store_memory({
            "content": content, "type": "context",
            "project": "test-017", "tags": ["pull-orch-test"],
            "connector_ids": ["test"]})
        record = await core.export_record({"id": sr["id"]})
        record["origin_node"] = fake_peer  # cross-peer arrival sim
        envelope = _SLACK.build_envelope_from_record(record, "test")
        if extra_envelope_fields:
            envelope.update(extra_envelope_fields)
        body = _SLACK.format_envelope(envelope)
        # Delete local source so receive doesn't see it as a dup-merge
        core.qdrant.delete(collection_name=core.COLLECTION,
                           points_selector=[sr["id"]])
        return body, envelope

    # ───────────────────────────────────────────────────────────────────
    # Case 1 — real peer envelope → stored
    # ───────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("Case 1: real peer envelope → stored")
    print("=" * 60)

    async def case1():
        content = f"pull-orch case 1 {uuid.uuid4()}"
        body, env = await make_real_envelope_body(content)
        r = await core.ingest_connector_message({
            "body": body, "connector_id": "test"})
        check("ingest real envelope → stored",
              r.get("status") == "stored", f"got: {r}")
        check("result has submitted_at for cursor advancement",
              bool(r.get("submitted_at")))
        if r.get("id"):
            cleanup_ids.append(r["id"])
        return body
    body1 = asyncio.run(case1())

    # ───────────────────────────────────────────────────────────────────
    # Case 2 — re-ingest same body → duplicate
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 2: re-ingest same body → duplicate")
    print("=" * 60)

    async def case2():
        r = await core.ingest_connector_message({
            "body": body1, "connector_id": "test"})
        check("re-ingest → duplicate", r.get("status") == "duplicate",
              f"got: {r}")
    asyncio.run(case2())

    # ───────────────────────────────────────────────────────────────────
    # Case 3 — non-envelope body (human message) → not_envelope (skip)
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 3: human-authored message → not_envelope (skip & continue)")
    print("=" * 60)

    async def case3():
        for body in (
            "Just a chat message, no envelope here.",
            "",
            "Maybe a code block ```python\nprint('hi')\n``` but no JSON envelope inside.",
        ):
            r = await core.ingest_connector_message({
                "body": body, "connector_id": "test"})
            check(f"non-envelope body ({body[:30]!r}) → not_envelope",
                  r.get("status") == "not_envelope", f"got: {r}")
    asyncio.run(case3())

    # ───────────────────────────────────────────────────────────────────
    # Case 4 — G11 smoke-test envelope → smoke_test_skipped (default)
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 4: is_smoke_test=true → smoke_test_skipped by default (G11)")
    print("=" * 60)

    async def case4():
        content = f"pull-orch case 4 SMOKE {uuid.uuid4()}"
        body, env = await make_real_envelope_body(
            content, extra_envelope_fields={"is_smoke_test": True})
        r = await core.ingest_connector_message({
            "body": body, "connector_id": "test"})
        check("smoke-test envelope → smoke_test_skipped (G11 default)",
              r.get("status") == "smoke_test_skipped", f"got: {r}")
        check("smoke-test result carries submitted_at",
              bool(r.get("submitted_at")))

        # Override → ingest succeeds
        r = await core.ingest_connector_message({
            "body": body, "connector_id": "test",
            "include_smoke_tests": True})
        check("include_smoke_tests=true bypasses G11 filter → stored",
              r.get("status") == "stored", f"got: {r}")
        if r.get("id"):
            cleanup_ids.append(r["id"])
    asyncio.run(case4())

    # ───────────────────────────────────────────────────────────────────
    # Case 5 — loopback (origin_node == self) → loopback_skipped
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 5: own envelope (origin_node == self) → loopback_skipped")
    print("=" * 60)

    async def case5():
        content = f"pull-orch case 5 SELF {uuid.uuid4()}"
        sr = await core.store_memory({
            "content": content, "type": "context",
            "project": "test-017", "connector_ids": ["test"]})
        cleanup_ids.append(sr["id"])
        record = await core.export_record({"id": sr["id"]})
        # No origin override — origin_node stays = self_node
        envelope = _SLACK.build_envelope_from_record(record, "test")
        body = _SLACK.format_envelope(envelope)
        r = await core.ingest_connector_message({
            "body": body, "connector_id": "test"})
        check("own envelope → loopback_skipped",
              r.get("status") == "loopback_skipped", f"got: {r}")
    asyncio.run(case5())

    # ───────────────────────────────────────────────────────────────────
    # Case 6 — tampered envelope → integrity_failed
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 6: tampered envelope (content mutated post-build) → integrity_failed")
    print("=" * 60)

    async def case6():
        content = f"pull-orch case 6 TAMPER {uuid.uuid4()}"
        sr = await core.store_memory({
            "content": content, "type": "context",
            "project": "test-017", "connector_ids": ["test"]})
        record = await core.export_record({"id": sr["id"]})
        record["origin_node"] = fake_peer
        envelope = _SLACK.build_envelope_from_record(record, "test")
        envelope["content"] = content + " <tampered>"
        body = _SLACK.format_envelope(envelope)
        core.qdrant.delete(collection_name=core.COLLECTION,
                           points_selector=[sr["id"]])
        r = await core.ingest_connector_message({
            "body": body, "connector_id": "test"})
        check("tampered envelope → integrity_failed",
              r.get("error") == "integrity_failed", f"got: {r}")
    asyncio.run(case6())

    # ───────────────────────────────────────────────────────────────────
    # Case 7 — config / argument validation
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 7: config / argument validation")
    print("=" * 60)

    async def case7():
        r = await core.ingest_connector_message({"body": "anything"})
        check("missing connector_id → missing_argument",
              r.get("error") == "missing_argument")
        r = await core.ingest_connector_message({"connector_id": "test"})
        check("missing body → missing_argument",
              r.get("error") == "missing_argument")
        r = await core.ingest_connector_message({
            "body": body1, "connector_id": "no-such-id"})
        check("unknown connector_id → connector_not_found",
              r.get("error") == "connector_not_found")
    asyncio.run(case7())

    # ───────────────────────────────────────────────────────────────────
    # Cleanup
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Cleanup")
    print("=" * 60)
    if cleanup_ids:
        core.qdrant.delete(collection_name=core.COLLECTION,
                           points_selector=cleanup_ids)
        print(f"  ✓ deleted {len(cleanup_ids)} test memories")
    for path in cleanup_paths:
        if os.path.exists(path):
            os.unlink(path)
    print(f"  ✓ removed {len(cleanup_paths)} temp config files")

    print()
    print("=" * 60)
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        sys.exit(1)
    print("ALL PULL-ORCHESTRATION TESTS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
