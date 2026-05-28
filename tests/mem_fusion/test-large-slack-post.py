#!/usr/bin/env python3
"""
Large Slack post — Rule 7 oversized-envelope summary-mode coverage.

When the canonical Slack envelope body is below ~12 KB the connector posts
it verbatim and the receiving peer can auto-ingest. When it exceeds that
threshold (long synthesis-style memories: daily briefs, weekly retros,
week-in-review reports) Slack rejects with `msg_too_long` even though
`build_connector_envelope` returned successfully. The mem-fusion-slack-
connector skill's Rule 7 says: produce a concise summary, append a footer
marker that names the canonical body size and the local memory id, and
post that instead. The local memory is untouched.

Cases exercised here:
  1. Small content (~400 chars) → envelope <12 KB → canonical mode is fine.
  2. Medium content (>3000 chars dense prose, single-chunk) → body still
     well above 3000 chars; canonical envelope round-trips via
     format → slack-normalize → parse → verify_integrity.
  3. Large content (~9 KB dense prose, multi-chunk) → canonical body
     exceeds the 12 KB summary-mode threshold; demonstrate the
     summary-mode wrap produces a bounded, well-formed Slack body
     that names the original envelope size and the memory id.

The test uses production helpers throughout (`core.store_memory`,
`core.export_record`, `SlackConnector.build_envelope_from_record`,
`SlackConnector.format_envelope`/`parse_envelope`/`verify_integrity`).
Created memories are tagged with a unique `project` and torn down at the
end so the local Qdrant is left clean.

Usage:
  ~/.local/share/mem-fusion/venv/bin/python tests/mem_fusion/test-large-slack-post.py
"""
import asyncio
import pathlib
import sys
import time
import uuid

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import core                            # noqa: E402
from connectors import get_connector   # noqa: E402

_SLACK = get_connector("slack")

# Rule 7 thresholds — canonical phrasing in
# src/skills/mem-fusion-slack-connector/SKILL.md "Oversized envelopes" section.
SUMMARY_MODE_THRESHOLD = 12_000   # body_size > this → switch to summary mode
SUMMARY_BODY_CAP       = 3_000    # summary text cap (before footer)
SUBSTRATE_HARD_CAP     = 38_000   # build_connector_envelope error cap


def slack_normalize(raw_body: str) -> str:
    """Simulate Slack's fence normalization (`json` hint + inside newlines stripped)."""
    return raw_body.replace("```json\n", "```").replace("\n```", "```")


def build_summary_post(summary_text: str, memory_id: str,
                       original_body_size: int) -> str:
    """Mechanical Rule 7 summary-mode wrap.

    The summarization itself is LLM creative work (preserve the headline,
    top 3-5 high-leverage items, risk callouts; drop per-meeting detail
    and link footers). This helper is the deterministic boundary: cap
    the summary text and append the canonical-envelope-size footer that
    receiving peers use to recognize a non-canonical post.

    Used both by the skill orchestration in production and by this test.
    """
    truncated = summary_text[:SUMMARY_BODY_CAP].rstrip()
    footer = (
        "\n\n---\n"
        f"_Summary post — canonical envelope was {original_body_size} chars, "
        f"exceeds Slack limit. Full content in local memory `{memory_id}`._"
    )
    return truncated + footer


async def _create_memory(content: str, project_tag: str) -> dict:
    sr = await core.store_memory({
        "content": content, "type": "context",
        "tags": ["large-post-test"], "project": project_tag, "importance": 3,
    })
    if sr.get("status") not in ("stored", "merged", "duplicate"):
        raise RuntimeError(f"store_memory failed: {sr}")
    return await core.export_record({"id": sr["id"]})


def _fake_remote(record: dict, peer: str) -> dict:
    """Confined hand-edit: simulate cross-peer arrival on a single test machine.

    Pattern matches test-connector-e2e-roundtrip.py's _as_remote_record.
    """
    out = dict(record)
    out["origin_node"] = peer
    return out


def main() -> None:
    project_tag = f"test-large-post-{int(time.time())}"
    created_ids: list[str] = []
    failures: list[str] = []
    fake_peer = f"fake-peer-{uuid.uuid4().hex[:8]}"

    def check(name: str, cond: bool, msg: str = "") -> None:
        prefix = "  ✓" if cond else "  ✗"
        print(f"{prefix} {name}{(' — ' + msg) if msg else ''}")
        if not cond:
            failures.append(name)

    print("=" * 60)
    print("test-large-slack-post — Rule 7 summary-mode coverage")
    print("=" * 60)
    print(f"project_tag: {project_tag}\nfake_peer:   {fake_peer}\n")

    # ── Case 1 — Small content, canonical envelope round-trips ───────────
    # Reality check: the 768-dim vector adds ~13 KB to every envelope, so
    # even a tiny post produces a ~14 KB body — over the 12 KB Rule 7
    # threshold. The test confirms the envelope shape is valid (parse +
    # integrity), not that small posts skip Rule 7. Rule 7 currently fires
    # on virtually all posts (documented in SKILL.md "Why this happens").
    print("=" * 60)
    print("Case 1: small content (~400 chars) → canonical envelope, round-trip")
    print("=" * 60)
    content1 = (
        "Short memory used to confirm canonical envelope renders + parses "
        "correctly for small posts. " + ("filler " * 30)
    )
    record1 = asyncio.run(_create_memory(content1, project_tag))
    created_ids.append(record1["id"])
    env1 = _SLACK.build_envelope_from_record(
        _fake_remote(record1, fake_peer), "test-canonical")
    body1 = _SLACK.format_envelope(env1)
    print(f"  content len = {len(content1)}, body_size = {len(body1)}")
    check("small post body well under substrate hard cap (38 KB)",
          len(body1) < SUBSTRATE_HARD_CAP,
          f"got body_size={len(body1)}")
    parsed1 = _SLACK.parse_envelope(slack_normalize(body1))
    check("canonical envelope round-trips via parse",
          isinstance(parsed1, dict) and parsed1.get("content") == content1,
          f"parsed type={type(parsed1).__name__}")
    if isinstance(parsed1, dict):
        ok1, det1 = _SLACK.verify_integrity(parsed1)
        check("canonical envelope integrity ok", ok1, str(det1) if det1 else "")

    # ── Case 2 — Medium content (>3000 chars), still canonical ────────────
    print()
    print("=" * 60)
    print("Case 2: medium content (>3000 chars, single-chunk) → canonical")
    print("=" * 60)
    # 3500+ chars of dense prose; stays a single chunk (chunk threshold 4000)
    paragraph = (
        "The mem-fusion connector pattern handles Slack posts well above "
        "3000 characters by including the full content inline in the "
        "canonical envelope. Receivers parse the fenced JSON and re-store "
        "the memory locally without re-embedding when the vector is "
        "carried in the envelope. "
    )
    content2 = (paragraph * 13).strip()  # ~3700 chars
    record2 = asyncio.run(_create_memory(content2, project_tag))
    created_ids.append(record2["id"])
    env2 = _SLACK.build_envelope_from_record(
        _fake_remote(record2, fake_peer), "test-canonical")
    body2 = _SLACK.format_envelope(env2)
    print(f"  content len = {len(content2)}, body_size = {len(body2)}")
    check("medium post content > 3000 chars",
          len(content2) > 3000,
          f"got content len={len(content2)}")
    check("medium post body > 3000 chars (a 'large' Slack post)",
          len(body2) > 3000,
          f"got body_size={len(body2)}")
    parsed2 = _SLACK.parse_envelope(slack_normalize(body2))
    check("medium canonical envelope round-trips byte-exact",
          isinstance(parsed2, dict) and parsed2.get("content") == content2,
          f"content len in={len(content2)}, "
          f"out={len(parsed2.get('content', '')) if isinstance(parsed2, dict) else 'n/a'}")
    if isinstance(parsed2, dict):
        ok2, det2 = _SLACK.verify_integrity(parsed2)
        check("medium canonical envelope integrity ok",
              ok2, str(det2) if det2 else "")

    # ── Case 3 — Large content (chunked, envelope >12 KB), summary mode ──
    # Sized to produce a body in the (12 KB, 38 KB) window — comfortably
    # over the Rule 7 threshold and clearly under the substrate hard cap.
    # body_size ≈ 2 * content_len + 13 KB vector + ~500 envelope overhead.
    # For body ≈ 25 KB target: content_len ≈ 5800. Use ~5500 to leave room.
    print()
    print("=" * 60)
    print("Case 3: ~5.5 KB dense content → body ~25 KB → summary mode")
    print("=" * 60)
    para = (
        "DAILY BRIEF — Long synthesis-style memory typical of weekly "
        "pulse / retro / week-in-review outputs. Multi-paragraph dense "
        "structure with KPI tables, named owners, action items, risk "
        "callouts. The genre that routinely produces canonical envelopes "
        "in the 25-35 KB range. "
    )
    content3 = ("DAILY BRIEF — opening headline. " + para * 20).strip()
    print(f"  prepared content len = {len(content3)}")
    record3 = asyncio.run(_create_memory(content3, project_tag))
    created_ids.append(record3["id"])
    env3 = _SLACK.build_envelope_from_record(
        _fake_remote(record3, fake_peer), "test-summary")
    body3 = _SLACK.format_envelope(env3)
    print(f"  canonical body_size = {len(body3)}")
    check("large canonical body exceeds 12 KB threshold (triggers Rule 7)",
          len(body3) > SUMMARY_MODE_THRESHOLD,
          f"got body_size={len(body3)}, threshold={SUMMARY_MODE_THRESHOLD}")
    check("large canonical body still under substrate hard cap (38 KB)",
          len(body3) < SUBSTRATE_HARD_CAP,
          f"got body_size={len(body3)}")

    # Summary text is LLM creative work in production; for the test we use
    # the first ~2800 chars of content as a stand-in. The test exercises
    # the WRAP shape (footer marker, cap, id reference) — not the LLM's
    # summarization quality.
    fake_summary = content3[:2800].rstrip()
    summary_post = build_summary_post(
        fake_summary, record3["id"], len(body3))
    print(f"  summary_post size = {len(summary_post)}")

    check("summary post is bounded (≤ SUMMARY_BODY_CAP + footer overhead)",
          len(summary_post) <= SUMMARY_BODY_CAP + 200,
          f"got {len(summary_post)}, expected ≤ {SUMMARY_BODY_CAP + 200}")
    check("summary post well under Slack hard cap",
          len(summary_post) < SUBSTRATE_HARD_CAP,
          f"got {len(summary_post)}")
    check("summary post names the canonical envelope size in footer",
          f"canonical envelope was {len(body3)} chars" in summary_post)
    check("summary post references the local memory id",
          f"`{record3['id']}`" in summary_post)
    check("summary post preserves the headline (first line of content)",
          summary_post.startswith("DAILY BRIEF"))
    check("summary post contains the Slack-limit explanation",
          "exceeds Slack limit" in summary_post)
    check("summary post separates summary from footer with `---`",
          "\n---\n" in summary_post)

    # Verify summary-mode does NOT modify the local memory.
    record3_after = asyncio.run(core.export_record({"id": record3["id"]}))
    check("local memory.content unchanged after summary-mode wrap",
          record3_after.get("content") == content3,
          f"len before={len(content3)}, after={len(record3_after.get('content', ''))}")

    # Sanity: a receiver running `parse_envelope` on the summary post
    # returns None (no fenced JSON envelope) — this is the documented
    # graceful skip for non-canonical posts.
    parsed_summary = _SLACK.parse_envelope(summary_post)
    check("summary post is correctly NOT recognized as canonical envelope",
          parsed_summary is None,
          f"got: {type(parsed_summary).__name__}")

    # ── Cleanup ──────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Cleanup")
    print("=" * 60)
    # core.delete_memory cascades to all chunks via canonical_id; raw
    # qdrant.delete would only catch chunk 0.
    deleted = 0
    for mid in created_ids:
        result = asyncio.run(core.delete_memory({"id": mid}))
        if result.get("status") == "deleted":
            deleted += 1
    print(f"  ✓ deleted {deleted}/{len(created_ids)} test memories")

    print()
    print("=" * 60)
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        sys.exit(1)
    print("ALL LARGE-SLACK-POST TESTS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
