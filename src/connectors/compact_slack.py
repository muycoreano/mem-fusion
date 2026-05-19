"""
CompactSlackConnector — batched + gzip + base64 wire format for Slack.

Implements the v0.6 personal-sync wire format per docs/v0.6_PERSONAL_SYNC.md.
Targets the personal-sync use case (member_count == 1 channels) where the
channel is pure substrate, not human-read. Achieves ~13× compression vs the
§5.1 JSON wire format on representative memory samples.

BODY SHAPE
----------
    [mf v0.6 compact n=<N> sha=<hex8>]
    <base64(gzip(json.dumps([env1, env2, ...], separators=(',',':'))))>

- Header line is a fast yes/no for parsers — non-matches return None.
- N is envelope count (telemetry; receiver doesn't depend on it).
- sha is sha256[:8] of the base64 payload — cheap transport-integrity
  stripe. Per-envelope content_hash remains authoritative; sha is just
  "did Slack mangle the body in transit" detection.

VECTOR HANDLING
---------------
Vectors are STRIPPED on send. Receivers re-embed via Ollama at ~25 ms per
envelope. This is the dominant compression lever (vectors are ~85% of
envelope bytes when included).

Rationale: for the personal-sync use case both endpoints already require
Ollama for `/remember` to work. The receive-side re-embed is free.

A future Format-D variant could keep int8-quantized vectors instead of
stripping; out of scope for v0.6.

CO-EXISTENCE WITH §5.1
----------------------
SlackConnector (§5.1 JSON) and CompactSlackConnector live side-by-side in
the registry. Orchestration picks per-channel based on sync.db member_count:
  member_count == 1   →  get_connector("slack-compact")  → CompactSlackConnector
  member_count >= 2   →  get_connector("slack")          → SlackConnector

Receivers iterating channel history try compact first (header-detect, fast
yes/no); on None fall through to SlackConnector. The same Slack channel can
mix both formats (e.g., across a membership transition); both parse.

BATCH-OF-ONE OK
---------------
A single-envelope batch is legitimate (e.g., when the flush worker fires
on a 30-second timer with only one memory buffered). The header still
prefixes with `n=1` and the body is a single-element JSON array.
"""
import base64
import gzip
import hashlib
import json
import re

from .envelope import Envelope
from .slack import SlackConnector

#: Header pattern. Match the start of a Slack body to fast-detect compact format.
#: Captures: (N, sha) — both for telemetry, sha for transport integrity check.
_HEADER_RE = re.compile(
    r"^\[mf v0\.6 compact n=(\d+) sha=([0-9a-f]{8})\]\n", re.MULTILINE
)


class CompactSlackConnector(SlackConnector):
    """Slack connector with compact batched wire format for personal-sync."""

    #: Distinct discriminator from "slack". Orchestration picks per-channel.
    type = "slack-compact"

    # ── Batch wire format I/O ──────────────────────────────────────────
    def format_batch(self, envelopes: list[Envelope]) -> str:
        """Construct one Slack body containing N envelopes.

        Vectors are stripped before serialization. Receivers re-embed.
        """
        if not envelopes:
            raise ValueError("format_batch requires at least one envelope")

        stripped = [
            {k: v for k, v in env.items() if k != "vector"}
            for env in envelopes
        ]
        payload_json = json.dumps(stripped, separators=(",", ":"), ensure_ascii=False)
        compressed   = gzip.compress(payload_json.encode("utf-8"), compresslevel=9)
        b64          = base64.b64encode(compressed).decode("ascii")
        sha8         = hashlib.sha256(b64.encode("ascii")).hexdigest()[:8]
        header       = f"[mf v0.6 compact n={len(envelopes)} sha={sha8}]"
        return f"{header}\n{b64}"

    def parse_batch(self, body: str) -> list[Envelope] | None:
        """Extract N envelopes from a compact-format Slack body.

        Returns None if the body isn't compact-format (no header) — caller
        should fall through to SlackConnector.parse_batch / parse_envelope
        for §5.1 JSON, or skip the message entirely (`not_envelope`).

        Transport-integrity check on the sha8 stripe is HARD: if the sha
        doesn't match the base64 payload, return None rather than try to
        parse — corruption mid-transit is real (Slack normalization,
        copy-paste through some clients). Per-envelope content_hash via
        verify_integrity is the load-bearing check; sha8 is the fast pre-check.
        """
        if not body:
            return None
        match = _HEADER_RE.match(body)
        if not match:
            return None

        expected_n   = int(match.group(1))
        expected_sha = match.group(2)

        # Body after header newline is the base64 payload.
        # Allow trailing whitespace; Slack sometimes appends a "Sent using" footer
        # on bot-posted messages (we strip everything after the b64 ends).
        payload_start = match.end()
        rest = body[payload_start:]

        # Take only the first contiguous base64 run; everything after is footer
        # (e.g., Slack's "*Sent using* <@...>" trailer when posted via MCP).
        b64_match = re.match(r"[A-Za-z0-9+/=]+", rest)
        if not b64_match:
            return None
        b64 = b64_match.group(0)

        # Transport integrity: sha8 of the base64 payload must match the header.
        actual_sha = hashlib.sha256(b64.encode("ascii")).hexdigest()[:8]
        if actual_sha != expected_sha:
            return None  # corrupted transit; receiver skips, no exception

        try:
            compressed   = base64.b64decode(b64, validate=True)
            payload_json = gzip.decompress(compressed).decode("utf-8")
            envelopes    = json.loads(payload_json)
        except (ValueError, OSError, json.JSONDecodeError):
            return None

        if not isinstance(envelopes, list):
            return None
        if not all(isinstance(e, dict) for e in envelopes):
            return None

        # n is telemetry; mismatch is suspicious but not fatal. Log to debug
        # via a sentinel field on the first envelope (caller-visible via parse).
        # For now, trust the actual list length; don't refuse on n mismatch
        # since envelopes is the ground truth.

        return envelopes

    # ── Singular methods (override to delegate to batch) ───────────────
    def format_envelope(self, envelope: Envelope) -> str:
        """Compact format always emits the batch shape; even N=1 is fine."""
        return self.format_batch([envelope])

    def parse_envelope(self, body: str) -> Envelope | None:
        """Return the first envelope of the batch (None if non-compact body)."""
        batch = self.parse_batch(body)
        if not batch:
            return None
        return batch[0]
