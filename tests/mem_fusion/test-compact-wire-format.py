#!/usr/bin/env python3
"""
v0.6 CompactSlackConnector wire-format unit tests.

Pure format I/O tests — hand-composed envelope dicts go in, encoded body
comes out, decoded list of envelopes comes back. No core / Qdrant / Ollama
dependency; runs fast (<1s).

Coverage:
  1.  Round-trip 1 envelope     → batch of [1], content byte-exact
  2.  Round-trip 10 envelopes   → batch of [10], all content byte-exact
  3.  Round-trip 50 envelopes   → batch of [50], all content byte-exact
  4.  Compact format strips vector field
  5.  format_envelope (singular) wraps as batch-of-1
  6.  parse_envelope (singular) returns first envelope of batch
  7.  Non-compact body → parse_batch returns None (no header)
  8.  §5.1 JSON body → CompactSlackConnector.parse_batch returns None
  9.  Compact body → SlackConnector.parse_envelope returns None (no fenced json)
  10. Empty/None body → parse_batch returns None
  11. Compact body with Slack "Sent using" trailer → still parses cleanly
  12. UTF-8 special chars (—, →, emoji) preserved byte-exact through round-trip
  13. Content containing triple-backtick markdown preserved (no fence collision)
  14. Tampered sha8 in header → parse_batch returns None (transport integrity)
  15. 50-envelope batch body fits under Slack §5.4 38 KB push-side cap
  16. content_hash matches verify_integrity post-round-trip
  17. Header n= mismatch with actual envelope count → still returns the actual count

Usage:
  ~/.local/share/mem-fusion/venv/bin/python tests/mem_fusion/test-compact-wire-format.py
"""
import hashlib
import pathlib
import sys
import unicodedata

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

from connectors.compact_slack import CompactSlackConnector
from connectors.slack import SlackConnector


SLACK_PUSH_CAP = 38_000  # §5.4 push-side cap

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}    {detail}")


def _wire_hash(content: str) -> str:
    norm = unicodedata.normalize("NFC", content)
    return "sha256:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()


def make_envelope(content: str, idx: int = 0, with_vector: bool = False) -> dict:
    env = {
        "envelope_version": 1,
        "content":          content,
        "content_hash":     _wire_hash(content),
        "origin_node":      f"test-peer-{idx % 3}",
        "submitted_at":     f"2026-05-19T20:00:{idx:02d}.000000+00:00",
        "type":             "context",
        "tags":             ["test", f"idx-{idx}"],
        "project":          "v0.6-compact-test",
        "importance":       3,
        "connector_ids":    ["test-compact"],
        "groups":           ["personal"],
    }
    if with_vector:
        env["vector"] = [0.01 * i for i in range(768)]
    return env


# ─── Test cases ─────────────────────────────────────────────────────────

def test_round_trip_n(n: int, label: str) -> None:
    print(f"\n── {label} ──")
    conn = CompactSlackConnector()
    envelopes = [make_envelope(f"memory content #{i} — with em-dash and → arrow", i)
                 for i in range(n)]

    body = conn.format_batch(envelopes)
    check(f"format_batch produces non-empty body for N={n}", len(body) > 0)
    check(f"body starts with compact header",
          body.startswith(f"[mf v0.6 compact n={n} sha="),
          f"body[:80]={body[:80]!r}")

    decoded = conn.parse_batch(body)
    check(f"parse_batch returns list", isinstance(decoded, list))
    check(f"parse_batch returns N={n} envelopes",
          decoded is not None and len(decoded) == n,
          f"got {len(decoded) if decoded else None}")

    if decoded is None or len(decoded) != n:
        return

    all_match = all(
        decoded[i]["content"] == envelopes[i]["content"]
        and decoded[i]["content_hash"] == envelopes[i]["content_hash"]
        and decoded[i]["origin_node"] == envelopes[i]["origin_node"]
        for i in range(n)
    )
    check(f"all N={n} envelopes byte-exact post-round-trip", all_match)


def test_strips_vector() -> None:
    print("\n── Vector stripping ──")
    conn = CompactSlackConnector()
    env = make_envelope("test", with_vector=True)
    check("source envelope has vector", "vector" in env and len(env["vector"]) == 768)

    body = conn.format_batch([env])
    decoded = conn.parse_batch(body)
    check("decoded envelope omits vector", decoded is not None and "vector" not in decoded[0])
    check("decoded content still byte-exact",
          decoded is not None and decoded[0]["content"] == env["content"])


def test_singular_methods() -> None:
    print("\n── Singular methods (format_envelope / parse_envelope) ──")
    conn = CompactSlackConnector()
    env = make_envelope("singular test")

    body = conn.format_envelope(env)
    check("format_envelope produces compact-shaped body",
          body.startswith("[mf v0.6 compact n=1 sha="))

    decoded = conn.parse_envelope(body)
    check("parse_envelope returns single dict (not list)",
          isinstance(decoded, dict) and decoded.get("content") == "singular test")


def test_non_compact_body() -> None:
    print("\n── Non-compact body handling ──")
    conn = CompactSlackConnector()

    check("None body → parse_batch returns None", conn.parse_batch(None) is None)
    check("empty body → parse_batch returns None", conn.parse_batch("") is None)
    check("plain text body → parse_batch returns None",
          conn.parse_batch("hello world, this is a normal slack message") is None)
    check("body with similar-looking prefix → parse_batch returns None",
          conn.parse_batch("[mf v0.5 something else]\nxxxx") is None)


def test_cross_format_isolation() -> None:
    print("\n── Cross-format isolation (§5.1 ↔ compact) ──")
    json_conn = SlackConnector()
    compact_conn = CompactSlackConnector()

    # SlackConnector format → CompactSlackConnector parse should return None
    env = make_envelope("cross format test")
    json_body = json_conn.format_envelope(env)
    check("§5.1 JSON body → CompactSlackConnector.parse_batch returns None",
          compact_conn.parse_batch(json_body) is None)

    # CompactSlackConnector format → SlackConnector parse should return None
    # (no fenced JSON block in compact body)
    compact_body = compact_conn.format_batch([env])
    parsed_by_json = json_conn.parse_envelope(compact_body)
    check("compact body → SlackConnector.parse_envelope returns None",
          parsed_by_json is None,
          f"got {type(parsed_by_json).__name__}")


def test_slack_sent_using_trailer() -> None:
    print("\n── Slack 'Sent using' trailer tolerance ──")
    conn = CompactSlackConnector()
    env = make_envelope("trailer test")
    body = conn.format_batch([env])
    # Simulate Slack's bot-message trailer appending
    body_with_trailer = body + "\n*Sent using* <@U0AJ63VCAJ1|Claude>"

    decoded = conn.parse_batch(body_with_trailer)
    check("body with trailing 'Sent using' parses cleanly",
          decoded is not None and len(decoded) == 1)
    check("content survives trailer noise",
          decoded is not None and decoded[0]["content"] == "trailer test")


def test_utf8_preservation() -> None:
    print("\n── UTF-8 preservation ──")
    conn = CompactSlackConnector()
    samples = [
        "em-dash — and en-dash – plus →",
        "emoji 🚀 in content 📊 should survive 🎉",
        "Unicode NFC: é (precomposed) vs é (combining)",  # combining
        "CJK: 你好世界",
        "Math: ∀x ∈ ℝ, x² ≥ 0",
    ]
    for s in samples:
        env = make_envelope(s)
        body = conn.format_batch([env])
        decoded = conn.parse_batch(body)
        ok = decoded is not None and decoded[0]["content"] == s
        check(f"round-trip preserves: {s[:40]!r}", ok)


def test_triple_backtick_content() -> None:
    print("\n── Triple-backtick markdown content (TD-4 regression) ──")
    conn = CompactSlackConnector()
    # Content with code fences — would collide with §5.1 fence-based parser,
    # but compact format uses header-detect not fence-detect, so this is fine.
    content = "Here's some code:\n```python\ndef foo():\n    pass\n```\nAnd here's more:\n```bash\necho hi\n```"
    env = make_envelope(content)
    body = conn.format_batch([env])
    decoded = conn.parse_batch(body)
    check("triple-backtick content round-trips byte-exact",
          decoded is not None and decoded[0]["content"] == content)


def test_tampered_sha8() -> None:
    print("\n── Tampered transport-integrity sha8 ──")
    conn = CompactSlackConnector()
    env = make_envelope("integrity test")
    body = conn.format_batch([env])

    # Mutate the sha8 in the header
    tampered = body.replace("sha=", "sha=00000000 ORIG_", 1)  # break it
    # Even more direct: rewrite the sha to all zeros while keeping body
    import re
    tampered = re.sub(r"sha=[0-9a-f]{8}", "sha=00000000", body, count=1)
    check("tampered sha8 → parse_batch returns None",
          conn.parse_batch(tampered) is None)


def test_size_under_slack_cap() -> None:
    print("\n── Size under Slack §5.4 cap (38 KB) ──")
    conn = CompactSlackConnector()
    # 50 envelopes with realistic content size (~1 KB each)
    envelopes = [make_envelope("x" * 800 + f" memory body #{i}", i) for i in range(50)]
    body = conn.format_batch(envelopes)
    size_bytes = len(body.encode("utf-8"))
    check(f"50-envelope batch ({size_bytes} bytes) under Slack cap (38000)",
          size_bytes < SLACK_PUSH_CAP,
          f"size={size_bytes} bytes")


def test_verify_integrity_after_round_trip() -> None:
    print("\n── verify_integrity after round-trip ──")
    conn = CompactSlackConnector()
    env = make_envelope("integrity round-trip")
    body = conn.format_batch([env])
    decoded = conn.parse_batch(body)
    assert decoded is not None and len(decoded) == 1
    ok, detail = conn.verify_integrity(decoded[0])
    check("verify_integrity passes on round-tripped envelope", ok, detail or "")


def test_n_mismatch_uses_actual() -> None:
    print("\n── Header n= vs actual envelope count ──")
    # If someone hand-edited the header n= but the body really has different
    # count, parser uses the actual list length, not the header value.
    conn = CompactSlackConnector()
    envelopes = [make_envelope(f"n-mismatch {i}", i) for i in range(3)]
    body = conn.format_batch(envelopes)
    # Mutate header to claim n=99
    import re
    fudged = re.sub(r"n=\d+", "n=99", body, count=1)
    # sha will mismatch the mutated header? actually no — the sha was computed
    # over the base64 payload, not the header. So sha still matches.
    decoded = conn.parse_batch(fudged)
    check("n= mismatch with actual count: parser still returns actual list",
          decoded is not None and len(decoded) == 3,
          f"got len={len(decoded) if decoded else None}")


# ─── Main ───────────────────────────────────────────────────────────────

def main() -> int:
    print("v0.6 CompactSlackConnector wire-format unit tests")
    print("=" * 60)

    test_round_trip_n(1, "Round-trip N=1")
    test_round_trip_n(10, "Round-trip N=10")
    test_round_trip_n(50, "Round-trip N=50")
    test_strips_vector()
    test_singular_methods()
    test_non_compact_body()
    test_cross_format_isolation()
    test_slack_sent_using_trailer()
    test_utf8_preservation()
    test_triple_backtick_content()
    test_tampered_sha8()
    test_size_under_slack_cap()
    test_verify_integrity_after_round_trip()
    test_n_mismatch_uses_actual()

    print()
    print("=" * 60)
    print(f"  PASS: {PASS}")
    print(f"  FAIL: {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
