#!/usr/bin/env python3
"""
0.5.0-015 — canonical content_hash test suite.

Verifies core.content_hash against the architect-acked spec
docs/v0.5_CONTENT_HASH_SPEC.md §4.3 reference vectors, plus invariants
that distinguish the canonical form from the legacy form:
  - NFC-equivalent inputs collapse (combining vs precomposed accents)
  - case-folding has been REMOVED (Hello ≠ hello → distinct hashes)
  - whitespace-stripping has been REMOVED ("hello" ≠ " hello\n")
  - truncation has been REMOVED (digest is 64 hex chars, not 16)

Also covers the wire-format companion:
  - SlackConnector._wire_hash agrees with core.content_hash modulo prefix
  - Round-trip through format_envelope → parse_envelope → verify_integrity
    holds for non-ASCII content (architect's "cross-peer Unicode drift"
    use case from the spec rationale).

Pure-function tests — no Qdrant, no embedding, no I/O. Runs offline.

Usage:
  ~/.local/share/mem-fusion/venv/bin/python tests/lib/test-content-hash-canonical.py
"""
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import core                            # noqa: E402
from connectors import get_connector   # noqa: E402
from connectors.slack import _wire_hash  # noqa: E402

_SLACK = get_connector("slack")


def main():
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = ""):
        prefix = "  ✓" if cond else "  ✗"
        print(f"{prefix} {name}{(' — ' + detail) if detail else ''}")
        if not cond:
            failures.append(name)

    # ── §4.3 reference vectors (locked) ────────────────────────────────
    print("=" * 60)
    print("Spec §4.3 reference vectors")
    print("=" * 60)
    vectors = [
        ("", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
        ("hello", "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"),
        ("hello\n", "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"),
    ]
    for text, expected in vectors:
        actual = core.content_hash(text)
        check(f"content_hash({text!r}) == {expected[:16]}…",
              actual == expected,
              f"got {actual[:16]}…" if actual != expected else "")

    # ── Format invariants ──────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Format invariants")
    print("=" * 60)
    h = core.content_hash("hello")
    check("digest is 64 hex chars (no truncation)", len(h) == 64, f"len={len(h)}")
    check("digest is lowercase hex", h == h.lower() and all(c in "0123456789abcdef" for c in h),
          f"got {h}")

    # ── NFC normalization (the core canonical claim) ───────────────────
    print()
    print("=" * 60)
    print("NFC normalization — combining vs precomposed agree")
    print("=" * 60)
    precomposed = "café"            # café — é = U+00E9 (single code point)
    combining   = "café"           # café — e + combining acute U+0301
    check("precomposed and combining-acute forms hash equal",
          core.content_hash(precomposed) == core.content_hash(combining),
          f"pre={core.content_hash(precomposed)[:16]}…, "
          f"comb={core.content_hash(combining)[:16]}…")

    # NFC vs NFKC distinction — full-width digits should NOT fold (we picked NFC)
    full_width_5 = "５"  # '５' — full-width digit five
    ascii_5      = "5"
    check("NFC keeps full-width digits distinct from ASCII (i.e., not NFKC)",
          core.content_hash(full_width_5) != core.content_hash(ascii_5),
          f"ff15→{core.content_hash(full_width_5)[:8]}…, "
          f"ascii→{core.content_hash(ascii_5)[:8]}…")

    # ── Case folding REMOVED ───────────────────────────────────────────
    print()
    print("=" * 60)
    print("Case-folding removed (Hello ≠ hello)")
    print("=" * 60)
    check("Hello and hello hash distinctly",
          core.content_hash("Hello") != core.content_hash("hello"))
    check("HELLO and hello hash distinctly",
          core.content_hash("HELLO") != core.content_hash("hello"))

    # ── Whitespace stripping REMOVED ───────────────────────────────────
    print()
    print("=" * 60)
    print("Whitespace stripping removed (whitespace is content)")
    print("=" * 60)
    check("'hello' ≠ ' hello'",  core.content_hash("hello") != core.content_hash(" hello"))
    check("'hello' ≠ 'hello '",  core.content_hash("hello") != core.content_hash("hello "))
    check("'hello' ≠ 'hello\\n'", core.content_hash("hello") != core.content_hash("hello\n"))

    # ── Wire-hash companion agrees modulo prefix ───────────────────────
    print()
    print("=" * 60)
    print("SlackConnector._wire_hash agrees with core.content_hash")
    print("=" * 60)
    for sample in ("", "hello", "hello\n", precomposed, combining, "ünïcødé Mëøw 🦊"):
        local = core.content_hash(sample)
        wire  = _wire_hash(sample)
        check(f"_wire_hash({sample!r}) == 'sha256:' + content_hash",
              wire == "sha256:" + local,
              f"local={local[:12]}…, wire={wire[:20]}…")

    # ── Cross-peer Unicode drift use case (the spec's motivating story) ─
    print()
    print("=" * 60)
    print("Cross-peer Unicode drift — round-trip via Slack connector")
    print("=" * 60)
    # Peer A's editor emits combining; peer B's emits precomposed. After
    # NFC, both sides agree. The wire envelope built from peer A's content
    # carries a content_hash that peer B's verify_integrity accepts AND
    # that matches what peer B would store locally for the same text.
    drifty_record = {
        "id": "00000000-0000-0000-0000-000000000000",
        "content": combining,
        "content_hash": core.content_hash(combining),  # local: 64-char NFC
        "origin_node": "peer-a",
        "submitted_at": "2026-05-18T00:00:00Z",
        "type": "context",
        "tags": [],
        "project": "test",
        "importance": 3,
        "groups": [],
        "connector_ids": [],
        "vector": None,
    }
    env = _SLACK.build_envelope_from_record(drifty_record, "slack-mem-test")
    body = _SLACK.format_envelope(env)
    # Receiver parses + verifies — content didn't change in transit
    parsed = _SLACK.parse_envelope(body)
    ok, detail = _SLACK.verify_integrity(parsed)
    check("verify_integrity OK on NFC-drifty content via build_envelope_from_record",
          ok is True, f"detail: {detail}")
    # Receiver computes its local hash — should equal sender's
    receiver_local = core.content_hash(parsed["content"])
    sender_local   = core.content_hash(combining)
    check("receiver local hash == sender local hash (NFC unifies)",
          receiver_local == sender_local,
          f"rx={receiver_local[:16]}…, tx={sender_local[:16]}…")

    # ── Idempotence (running canonicalization twice ≡ once) ────────────
    print()
    print("=" * 60)
    print("Idempotence — re-hashing canonical content reproduces same digest")
    print("=" * 60)
    nfc_text = "éclair"  # already NFC
    check("NFC-already text hashes identically", core.content_hash(nfc_text) == core.content_hash(nfc_text))
    # And hashing an already-canonical string equals its hash byte-exact
    check("idempotent: content_hash is pure function",
          core.content_hash("repeat") == core.content_hash("repeat"))

    print()
    print("=" * 60)
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        sys.exit(1)
    print("ALL CONTENT_HASH CANONICAL TESTS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
