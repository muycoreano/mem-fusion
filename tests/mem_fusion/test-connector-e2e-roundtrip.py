#!/usr/bin/env python3
"""
v0.5 Slack connector e2e round-trip test.

Exercises the FULL production chain:

  core.store_memory          (real local insert)
    → core.export_record     (real record extraction)
    → SlackConnector.build_envelope_from_record
                             (production sender-side helper)
    → SlackConnector.format_envelope
    → Slack-normalization filter (simulates what slack_read_channel returns)
    → SlackConnector.parse_envelope
    → SlackConnector.verify_integrity   (via core.store_memory_from_envelope)
    → core.store_memory_from_envelope   (real receive-side ingest)

NO HAND-COMPOSED ENVELOPES. NO HAND-EDITED TEST DATA EXCEPT WHERE
LITERALLY NECESSARY — case 1's origin_node override is the only such
edit, and it's confined to ONE field with an explicit comment, because
simulating cross-peer arrival on a single machine has no production
helper for "create a record with a different origin_node".

Cases:
  1. Peer envelope, fresh content       → stored
  2. Same peer envelope re-ingested     → duplicate
  3. Same peer, additional connector_id → merged (additive widening)
  4. Own envelope (origin == self)      → loopback_skipped
  5. Tampered envelope (hash mismatch)  → integrity_failed
  6. Envelope missing required field    → missing_field error
  7. Envelope with valid 768-dim vector → stored, vector preserved
                                          (no re-embedding via Ollama)
  8. BACK-COMPAT REGRESSION: pre-refactor body shapes captured from the
     production #mf-test-connector channel. Architect-required (staff
     audit sign-off 2026-05-18) — verifies the Stage 1 refactor's
     parse_envelope + verify_integrity continue to handle:
       8a. Pre-§5.1 simplification (header line + `content_hash:` line)
       8b. Simplified §5.1 (current; Slack-normalized fence)

Runs against the LOCAL running Qdrant; uses a unique project tag for
isolation and cleans up created memories before exit. Exit code 0 on
all-pass, 1 on failure.

Usage:
  ~/.local/share/mem-fusion/venv/bin/python tests/mem_fusion/test-connector-e2e-roundtrip.py
"""
import asyncio
import math
import pathlib
import sys
import time
import uuid

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import core                            # noqa: E402
from connectors import get_connector   # noqa: E402

#: Single source of truth — the same instance every test step uses.
_SLACK = get_connector("slack")


def slack_normalize(raw_body: str) -> str:
    """Simulate Slack's fence normalization (strip `json` hint + inside newlines).

    This is what `slack_read_channel` returns AFTER Slack's server-side
    normalization. The connector parser is tolerant of both forms; this
    function exercises the harder of the two.
    """
    return raw_body.replace("```json\n", "```").replace("\n```", "```")


async def _create_local_record(content: str, project_tag: str) -> dict:
    """Production-helper-only path: store a local memory and export the record.

    Returns the record exactly as `export_record` would yield it on the
    receiving peer — including the just-set origin_node (this machine's
    node_name) and the freshly-computed vector.
    """
    sr = await core.store_memory({
        "content": content, "type": "context",
        "tags": ["e2e-test"], "project": project_tag, "importance": 3,
    })
    if sr.get("status") not in ("stored", "merged", "duplicate"):
        raise RuntimeError(f"store_memory failed: {sr}")
    return await core.export_record({"id": sr["id"]})


def _as_remote_record(record: dict, fake_origin: str) -> dict:
    """Return a copy of `record` with origin_node set to a simulated peer.

    Confined hand-edit: on a single test machine there's no production
    primitive that creates a record "as if it came from another peer".
    This swap is the minimum surface change needed to exercise the
    cross-peer code paths (loopback bypass, integrity check on remote
    content, dedup on inbound).
    """
    out = dict(record)
    out["origin_node"] = fake_origin
    return out


def _delete_local(memory_id: str) -> None:
    """Delete a local record between export and re-ingest.

    Required for "fresh peer envelope → stored" semantics: on a real
    cross-peer pull, the receiver does NOT have the content beforehand.
    On a single-machine test we have to create-export-delete to simulate
    that arrival shape, otherwise receive-side content_hash dedup
    correctly returns `merged` (the content is already here from the
    sender-side store) and we'd wrongly conclude the receive path is
    broken.
    """
    core.qdrant.delete(collection_name=core.COLLECTION,
                       points_selector=[memory_id])


# ── Back-compat regression bodies (captured from production channel) ────────
#
# These are VERBATIM message bodies pulled from #mf-test-connector via
# slack_read_channel at 2026-05-18 ahead of the Stage 1 refactor. They
# represent the two on-the-wire shapes the refactor MUST continue to
# parse + integrity-check correctly:
#
#   8a — pre-§5.1 simplification (2026-05-17 20:44 EDT):
#        Header line + separate `content_hash:` line + blank + content +
#        fenced JSON. This format was on the wire before the §5.1 cleanup.
#
#   8b — simplified §5.1 (2026-05-17 22:38 EDT):
#        Just content + fenced JSON. Current format; Slack-normalized
#        fence (no `json` hint, no inside-fence newlines).
#
# Architect-required (audit sign-off 2026-05-18): if either fails, Stage 1
# silently broke pull from any pre-refactor message in the channel.
PRE_REFACTOR_BODY_8A = (
    "[mem-fusion 0.5] mc-macbookair · context · 2026-05-18T00:43:48.635123+00:00 · test\n"
    "content_hash: sha256:464b8ecfe64e40c293c63d01ba60a31d4191116c4084bd3b6e2c33ea5688e220\n"
    "\n"
    "E-D smoke test from mc-macbookair (Claude-Engineer) — verifying v0.5 wire format round-trip after E-A/B/C land. Test timestamp: 2026-05-18T00:43:48.635123+00:00\n"
    "\n"
    "```{\n"
    "  \"envelope_version\": 1,\n"
    "  \"content_hash\": \"sha256:464b8ecfe64e40c293c63d01ba60a31d4191116c4084bd3b6e2c33ea5688e220\",\n"
    "  \"origin_node\": \"mc-macbookair\",\n"
    "  \"submitted_at\": \"2026-05-18T00:43:48.635123+00:00\",\n"
    "  \"content\": \"E-D smoke test from mc-macbookair (Claude-Engineer) — verifying v0.5 wire format round-trip after E-A/B/C land. Test timestamp: 2026-05-18T00:43:48.635123+00:00\",\n"
    "  \"type\": \"context\",\n"
    "  \"tags\": [\n"
    "    \"v0.5\",\n"
    "    \"smoke-test\",\n"
    "    \"e-d\",\n"
    "    \"from-claude-engineer\"\n"
    "  ],\n"
    "  \"project\": \"mem-fusion\",\n"
    "  \"importance\": 3,\n"
    "  \"connector_ids\": [\n"
    "    \"test\"\n"
    "  ],\n"
    "  \"groups\": []\n"
    "}```"
)

PRE_REFACTOR_BODY_8B = (
    "E-D-redux smoke test from mc-macbookair (Claude-Engineer) — validating the simplified §5.1 wire format via the new connectors/ package (SlackConnector.format_envelope). No more header, no separate content_hash line — just content + fenced JSON envelope. Test ts: 2026-05-18T02:37:53.399648+00:00\n"
    "\n"
    "```{\n"
    "  \"envelope_version\": 1,\n"
    "  \"content_hash\": \"sha256:8eee47dc3e352bb5d1829636467ca0511a72470e7c96148f2173eb2e9a596257\",\n"
    "  \"origin_node\": \"mc-macbookair\",\n"
    "  \"submitted_at\": \"2026-05-18T02:37:53.399648+00:00\",\n"
    "  \"content\": \"E-D-redux smoke test from mc-macbookair (Claude-Engineer) — validating the simplified §5.1 wire format via the new connectors/ package (SlackConnector.format_envelope). No more header, no separate content_hash line — just content + fenced JSON envelope. Test ts: 2026-05-18T02:37:53.399648+00:00\",\n"
    "  \"type\": \"context\",\n"
    "  \"tags\": [\n"
    "    \"v0.5\",\n"
    "    \"e-d-redux\",\n"
    "    \"simplified-format\",\n"
    "    \"is_smoke_test\"\n"
    "  ],\n"
    "  \"project\": \"mem-fusion-e2e-test\",\n"
    "  \"importance\": 3,\n"
    "  \"connector_ids\": [\n"
    "    \"test\"\n"
    "  ],\n"
    "  \"is_smoke_test\": true\n"
    "}```"
)


def main():
    project_tag = f"test-connector-e2e-{int(time.time())}"
    created_ids: list[str] = []
    failures: list[str] = []

    def check(name: str, cond: bool, msg: str = ""):
        prefix = "  ✓" if cond else "  ✗"
        print(f"{prefix} {name}{(' — ' + msg) if msg else ''}")
        if not cond:
            failures.append(name)

    self_node = core._resolve_node_name()
    print(f"SELF_NODE: {self_node}\n")

    fake_peer = f"fake-peer-{uuid.uuid4().hex[:8]}"

    # ───────────────────────────────────────────────────────────────────
    # Case 1 — peer envelope, fresh content → stored
    # Production-helper chain: store_memory → export_record →
    # build_envelope_from_record → format_envelope → slack_normalize →
    # parse_envelope → store_memory_from_envelope.
    # ───────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("Case 1: peer envelope, fresh content → stored (full production chain)")
    print("=" * 60)
    content1 = f"e2e case 1 from peer-x: fresh content {uuid.uuid4()}"
    record1 = asyncio.run(_create_local_record(content1, project_tag))
    remote_record1 = _as_remote_record(record1, fake_peer)
    envelope1 = _SLACK.build_envelope_from_record(remote_record1, "test")
    body1 = slack_normalize(_SLACK.format_envelope(envelope1))
    # Simulate cross-peer arrival: delete local source so the receive
    # path encounters this content for the first time (true "stored").
    _delete_local(record1["id"])
    parsed1 = _SLACK.parse_envelope(body1)
    check("parse_envelope round-trips through Slack normalization",
          isinstance(parsed1, dict) and parsed1.get("content") == content1)
    check("parsed envelope has wire-format content_hash from build_envelope_from_record",
          parsed1.get("content_hash", "").startswith("sha256:") and
          len(parsed1.get("content_hash", "")) == 71,  # "sha256:" + 64 hex
          f"got: {parsed1.get('content_hash', '')[:30]}…")

    r1 = asyncio.run(core.store_memory_from_envelope(parsed1, "slack"))
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
    parsed2 = _SLACK.parse_envelope(body1)
    r2 = asyncio.run(core.store_memory_from_envelope(parsed2, "slack"))
    check("re-ingest same content → duplicate",
          r2.get("status") == "duplicate", f"got: {r2}")
    check("duplicate.id matches first store",
          r2.get("id") == r1.get("id"),
          f"got: {r2.get('id')}, expected {r1.get('id')}")

    # ───────────────────────────────────────────────────────────────────
    # Case 3 — same content, additional connector_id → merged
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 3: same content, additional connector_id → merged (additive)")
    print("=" * 60)
    envelope3 = _SLACK.build_envelope_from_record(remote_record1, "test-channel-2")
    parsed3 = _SLACK.parse_envelope(slack_normalize(_SLACK.format_envelope(envelope3)))
    r3 = asyncio.run(core.store_memory_from_envelope(parsed3, "slack"))
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
    content4 = f"e2e case 4 from self: should be loopback-skipped {uuid.uuid4()}"
    record4 = asyncio.run(_create_local_record(content4, project_tag))
    if record4.get("id"):
        created_ids.append(record4["id"])
    # No _as_remote_record: keep origin_node = self_node so loopback triggers.
    envelope4 = _SLACK.build_envelope_from_record(record4, "test")
    parsed4 = _SLACK.parse_envelope(slack_normalize(_SLACK.format_envelope(envelope4)))
    r4 = asyncio.run(core.store_memory_from_envelope(parsed4, "slack"))
    check("envelope from self → loopback_skipped",
          r4.get("status") == "loopback_skipped", f"got: {r4}")

    # ───────────────────────────────────────────────────────────────────
    # Case 5 — tampered envelope (hash mismatch) → integrity_failed
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 5: tampered envelope (content mutated after build) → integrity_failed")
    print("=" * 60)
    content5 = f"e2e case 5 tampered {uuid.uuid4()}"
    record5 = asyncio.run(_create_local_record(content5, project_tag))
    if record5.get("id"):
        created_ids.append(record5["id"])
    envelope5 = _SLACK.build_envelope_from_record(
        _as_remote_record(record5, fake_peer), "test")
    # TAMPER: mutate content after build → wire hash no longer matches.
    envelope5["content"] = content5 + " <tampered>"
    parsed5 = _SLACK.parse_envelope(slack_normalize(_SLACK.format_envelope(envelope5)))
    r5 = asyncio.run(core.store_memory_from_envelope(parsed5, "slack"))
    check("tampered envelope → integrity_failed",
          r5.get("error") == "integrity_failed", f"got: {r5}")
    check("integrity_failed detail names content_hash mismatch",
          "content_hash" in r5.get("detail", "").lower(),
          f"detail: {r5.get('detail', '')}")

    # ───────────────────────────────────────────────────────────────────
    # Case 6 — missing required field → missing_field error
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 6: envelope missing required field → missing_field error")
    print("=" * 60)
    content6 = f"e2e case 6 missing-field {uuid.uuid4()}"
    record6 = asyncio.run(_create_local_record(content6, project_tag))
    if record6.get("id"):
        created_ids.append(record6["id"])
    envelope6 = _SLACK.build_envelope_from_record(
        _as_remote_record(record6, fake_peer), "test")
    envelope6.pop("origin_node")
    parsed6 = _SLACK.parse_envelope(slack_normalize(_SLACK.format_envelope(envelope6)))
    r6 = asyncio.run(core.store_memory_from_envelope(parsed6, "slack"))
    check("missing origin_node → missing_field error",
          r6.get("error") == "missing_field", f"got: {r6}")

    # ───────────────────────────────────────────────────────────────────
    # Case 7 — envelope with valid 768-dim vector → stored, vector preserved
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 7: envelope carries 768-dim vector → stored without re-embed")
    print("=" * 60)
    content7 = f"e2e case 7 vector-carried {uuid.uuid4()}"
    record7 = asyncio.run(_create_local_record(content7, project_tag))
    # The exported record already carries the real vector — pass through.
    envelope7 = _SLACK.build_envelope_from_record(
        _as_remote_record(record7, fake_peer), "test")
    check("envelope includes vector from record",
          isinstance(envelope7.get("vector"), list) and len(envelope7["vector"]) == 768)
    sent_vec = list(envelope7["vector"])
    parsed7 = _SLACK.parse_envelope(slack_normalize(_SLACK.format_envelope(envelope7)))
    # Simulate cross-peer arrival (see Case 1) — delete local source so
    # the receive path stores the vector rather than dedup-merging.
    _delete_local(record7["id"])
    r7 = asyncio.run(core.store_memory_from_envelope(parsed7, "slack"))
    check("envelope with valid vector → stored",
          r7.get("status") == "stored", f"got: {r7}")
    if r7.get("id"):
        created_ids.append(r7["id"])
    # Qdrant L2-normalizes vectors on store for Cosine collections.
    # The proof that the envelope's vector was USED (not re-embedded):
    # stored == L2-normalize(sent). Re-embedding would produce a different
    # vector that wouldn't match the unit-norm of sent.
    if r7.get("id"):
        pts = core.qdrant.retrieve(collection_name=core.COLLECTION,
                                   ids=[r7["id"]], with_vectors=True)
        if pts:
            stored_vec = list(pts[0].vector or [])
            norm = math.sqrt(sum(x * x for x in sent_vec))
            sent_unit = [x / norm for x in sent_vec] if norm > 0 else sent_vec
            vec_matches = (len(stored_vec) == 768 and
                           all(abs(a - b) < 1e-5 for a, b in zip(stored_vec, sent_unit)))
            check("stored vector == L2-normalize(sent) → vector path used, no re-embed",
                  vec_matches,
                  f"len={len(stored_vec)}, match={'yes' if vec_matches else 'no'}")

    # ───────────────────────────────────────────────────────────────────
    # Case 8 — BACK-COMPAT REGRESSION (architect-required)
    # Pre-refactor bodies captured verbatim from #mf-test-connector.
    # Stage 1's refactor MUST continue to parse + integrity-check these.
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 8: back-compat regression on pre-refactor body shapes")
    print("=" * 60)
    # 8a — pre-§5.1 with header + content_hash: line
    parsed_8a = _SLACK.parse_envelope(PRE_REFACTOR_BODY_8A)
    check("8a parse_envelope extracts JSON from pre-§5.1 body",
          isinstance(parsed_8a, dict) and parsed_8a.get("envelope_version") == 1,
          f"got: {type(parsed_8a).__name__}")
    if isinstance(parsed_8a, dict):
        ok_8a, detail_8a = _SLACK.verify_integrity(parsed_8a)
        check("8a verify_integrity on pre-§5.1 body → ok",
              ok_8a is True,
              f"detail: {detail_8a}")

    # 8b — simplified §5.1, Slack-normalized fence
    parsed_8b = _SLACK.parse_envelope(PRE_REFACTOR_BODY_8B)
    check("8b parse_envelope extracts JSON from simplified §5.1 body",
          isinstance(parsed_8b, dict) and parsed_8b.get("envelope_version") == 1,
          f"got: {type(parsed_8b).__name__}")
    if isinstance(parsed_8b, dict):
        ok_8b, detail_8b = _SLACK.verify_integrity(parsed_8b)
        check("8b verify_integrity on simplified §5.1 body → ok",
              ok_8b is True,
              f"detail: {detail_8b}")

    # ───────────────────────────────────────────────────────────────────
    # Case 9 — TD-4 REGRESSION: memory content containing markdown code
    # fences must not collide with the envelope fence parser. Pre-fix,
    # `parse_envelope` used `find("```")` for open and `rfind("```")`
    # for close — content with `\`\`\`code\`\`\`` blocks would have
    # find() match the content's open-fence, breaking JSON extraction.
    # Post-fix uses rfind for both, locating the LAST fenced block.
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case 9: code-fence content does not collide with envelope parser (TD-4)")
    print("=" * 60)

    # Build a record whose content contains its OWN triple-backtick block.
    content9 = (
        f"e2e case 9 code-fence-content {uuid.uuid4()}\n"
        "\n"
        "Here is some example code:\n"
        "\n"
        "```python\n"
        "def hello():\n"
        "    return 'world'\n"
        "```\n"
        "\n"
        "And another block:\n"
        "\n"
        "```bash\n"
        "echo hi\n"
        "```\n"
        "\n"
        "End of content."
    )
    rec9 = asyncio.run(_create_local_record(content9, project_tag))
    created_ids.append(rec9["id"])
    rec9_remote = _as_remote_record(rec9, fake_peer)
    env9 = _SLACK.build_envelope_from_record(rec9_remote, "test-td4")
    body9 = _SLACK.format_envelope(env9)
    body9_normalized = slack_normalize(body9)

    parsed_9 = _SLACK.parse_envelope(body9_normalized)
    check("9 parse_envelope extracts JSON despite content code fences",
          isinstance(parsed_9, dict) and parsed_9.get("envelope_version") == 1,
          f"got: {type(parsed_9).__name__} — wrong block matched")

    if isinstance(parsed_9, dict):
        ok_9, detail_9 = _SLACK.verify_integrity(parsed_9)
        check("9 verify_integrity on code-fence-content envelope → ok",
              ok_9 is True,
              f"detail: {detail_9}")
        check("9 envelope.content survived round-trip byte-exact",
              parsed_9.get("content") == content9,
              f"len diff: in={len(content9)}, out={len(parsed_9.get('content', ''))}")

    # Local source for case 9 stays for cleanup; remote ingest not exercised
    # here (the parser fix is the only TD-4 concern; the rest of the receive
    # path is already verified by Cases 1-3).

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
