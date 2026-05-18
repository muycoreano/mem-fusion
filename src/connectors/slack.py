"""
SlackConnector — wire-format adapter for Slack channels.

Implements the v0.5 Connector interface for the Slack substrate per
docs/v0.5_CONNECTOR_ARCHITECTURE.md §5.1 (simplified 2026-05-17).

BODY SHAPE
----------
    <human-readable content>

    ```json
    {<envelope JSON>}
    ```

No header line, no separate `content_hash:` line. The leading content is
purely for humans scrolling the channel; the JSON envelope is the single
source of truth for every machine-consumed field (origin, hash, type,
connector_ids, etc.).

SLACK FENCE NORMALIZATION
-------------------------
Slack normalizes fenced code blocks on ingest:
  - Strips the `json` language hint
  - Strips the newline after the opening fence
  - Strips the newline before the closing fence
JSON content BETWEEN the fences is preserved byte-exact. The parser uses
string-find + json.loads (not a strict fence regex) so it tolerates both:
  - Sender-side raw form:   `` ```\\njson\\n{...}\\n``` ``
  - Slack-normalized form:  `` ```{...}``` ``

INTEGRITY SCHEME (per §5.1)
---------------------------
    content_hash = "sha256:" + sha256(content.encode("utf-8")).hexdigest()

Note: this is the WIRE-FORMAT hash, distinct from the local-canonical
hash `core.content_hash(text)` (16-char strip+lower, pre-0.5.0-015).
Receivers verify the wire hash via `verify_integrity` before any local
processing; the local hash is used downstream for receiver-side dedup.

0.5.0-015 will unify the local hash to the canonical NFC+UTF-8+sha256[64]
form; this connector's wire hash will then match the canonical form
byte-for-byte. Today they diverge intentionally; the audit calls this
out as a Stage 1.3 fix.
"""
import hashlib
import json

from .base import Connector
from .envelope import CURRENT_ENVELOPE_VERSION, Envelope, REQUIRED_FIELDS


def _wire_hash(content: str) -> str:
    """Compute the Slack §5.1 wire-format content_hash for `content`.

    Single source of truth for both `verify_integrity` (receive-side
    verification) and `build_envelope_from_record` (sender-side
    population). Centralized so the two stay in lock-step — changing
    the scheme means changing one function, not two parallel call sites.
    """
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


class SlackConnector(Connector):
    """Connector for Slack channels per v0.5 §5.1."""

    type = "slack"

    # ── Wire format I/O ────────────────────────────────────────────────
    def format_envelope(self, envelope: Envelope) -> str:
        """Construct a Slack message body from a canonical envelope.

        Sender-side always emits the `json` language hint and inside-fence
        newlines (more readable in raw paste). Slack normalizes on ingest;
        JSON content round-trips byte-exact through `parse_envelope`.

        Body-size guard: callers SHOULD check the rendered length against
        the §5.4 push-side cap (~38 KB practical; 40 KB Slack ceiling).
        This function does not enforce — body-size is an orchestration
        concern; the connector renders unconditionally.
        """
        content  = envelope.get("content", "")
        env_json = json.dumps(envelope, indent=2, ensure_ascii=False)
        return f"{content}\n\n```json\n{env_json}\n```"

    def parse_envelope(self, body: str) -> Envelope | None:
        """Extract the JSON envelope from a Slack message body per §5.1.

        Returns the parsed envelope, or None if no fenced block is present
        or the JSON is malformed. None is the normal "this isn't connector
        traffic" signal — receivers skip and continue.
        """
        if not body:
            return None
        open_idx = body.find("```")
        close_idx = body.rfind("```")
        if open_idx < 0 or close_idx <= open_idx:
            return None
        envelope_text = body[open_idx + 3:close_idx].lstrip()
        if envelope_text.startswith("json"):
            envelope_text = envelope_text[4:].lstrip()
        try:
            parsed = json.loads(envelope_text)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    # ── Integrity (receive-side) ───────────────────────────────────────
    def verify_integrity(self, envelope: Envelope) -> tuple[bool, str | None]:
        """Verify Slack §5.1 wire-format integrity.

        Returns:
          (True,  None)     — content_hash matches sha256(content).
          (False, detail)   — mismatch; detail names the expected vs received
                              prefix for quick triage (full hashes too long
                              for one-line surfacing).

        Pre-flight checks: required fields presence + types. A missing-
        content envelope is reported as integrity_failed with a clear
        detail, not silently bypassed — only `verify_integrity == ok`
        permits downstream persistence.
        """
        if not isinstance(envelope, dict):
            return False, f"envelope is not a dict: {type(envelope).__name__}"
        for field in REQUIRED_FIELDS:
            if field not in envelope:
                return False, f"missing required field: {field}"
        content = envelope.get("content")
        wire    = envelope.get("content_hash")
        if not isinstance(content, str):
            return False, "envelope.content must be str"
        if not isinstance(wire, str):
            return False, "envelope.content_hash must be str"
        expected = _wire_hash(content)
        if wire != expected:
            return False, (
                f"content_hash does not match sha256(content); "
                f"expected={expected[:24]}…, received={wire[:24]}…"
            )
        return True, None

    # ── Envelope construction (sender-side) ────────────────────────────
    def build_envelope_from_record(self,
                                   record: dict,
                                   connector_id: str) -> Envelope:
        """Construct a Slack-format Envelope from a local memory record.

        See Connector.build_envelope_from_record for the contract.

        Implementation notes:
          - content_hash is computed FRESH from record["content"]; we do
            NOT trust record["content_hash"] (which is the local-canonical
            16-char form, not the wire-format full sha256).
          - connector_ids: union(record's connector_ids, [connector_id]).
            Idempotent: re-sending an entry through the same connector
            doesn't grow the list.
          - origin_node: verbatim from record — post-0.5.0-012, every local
            store sets this to config.node_name. Falsy values are an error
            condition the caller should catch (we do NOT silently fall back
            to local node_name, which would mis-attribute pulled memories).
          - vector: included if record["vector"] is a 768-dim list. Avoids
            receiver re-embed.
          - groups: pass-through. v0.5 ignores groups at scope-determination
            but the payload survives for legacy read paths.
        """
        if not isinstance(record, dict):
            raise TypeError(f"record must be dict, got {type(record).__name__}")
        if not isinstance(connector_id, str) or not connector_id:
            raise ValueError("connector_id must be a non-empty str")

        content = record.get("content", "")
        if not isinstance(content, str) or not content:
            raise ValueError("record.content must be a non-empty str")

        origin = record.get("origin_node", "")
        if not isinstance(origin, str) or not origin:
            raise ValueError(
                "record.origin_node missing or empty; expected post-0.5.0-012 "
                "stores to populate node_name. Re-store the memory or back-fill."
            )

        submitted_at = record.get("submitted_at") or record.get("timestamp", "")
        if not isinstance(submitted_at, str) or not submitted_at:
            raise ValueError("record.submitted_at (or .timestamp) missing")

        existing_cids = record.get("connector_ids") or []
        if not isinstance(existing_cids, list):
            existing_cids = []
        merged_cids = list(existing_cids)
        if connector_id not in merged_cids:
            merged_cids.append(connector_id)

        envelope: Envelope = {
            "envelope_version": CURRENT_ENVELOPE_VERSION,
            "content":          content,
            "content_hash":     _wire_hash(content),
            "origin_node":      origin,
            "submitted_at":     submitted_at,
            "type":             record.get("type", ""),
            "tags":             list(record.get("tags") or []),
            "project":          record.get("project", ""),
            "importance":       record.get("importance", 3),
            "connector_ids":    merged_cids,
            "groups":           list(record.get("groups") or []),
        }

        vec = record.get("vector")
        if isinstance(vec, list) and len(vec) > 0 and all(isinstance(x, (int, float)) for x in vec):
            envelope["vector"] = vec

        return envelope
