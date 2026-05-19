"""
Connector ABC — interface every connector substrate implements.

A Connector is the WIRE-FORMAT ADAPTER for a productivity-tool substrate
(Slack, GDrive, Teams, Discord, Notion, etc.). It is responsible for:

  - Translating between canonical Envelopes and substrate message bodies
    (`format_envelope`, `parse_envelope`).
  - Owning the integrity scheme its wire format declares (`verify_integrity`).
  - Producing a canonical Envelope from a local memory record + the
    connector-id the user is routing through (`build_envelope_from_record`).

It does NOT:
  - Make API calls (those happen in skill orchestration via Claude's MCP).
  - Maintain cursors or per-connector state (that's core.get/set_connector_cursor).
  - Persist memories (that's core.store_memory_from_envelope, which takes
    the parsed envelope + a connector instance and is substrate-agnostic
    in everything except calling connector.verify_integrity).

This narrow contract is what lets v0.5 ship one connector cleanly and
v0.6+ add more without core.py churn. core.py never imports specific
connector modules — it imports the package-level get_connector() registry
and asks for whichever type is needed at runtime.

INTERFACE EVOLUTION (per architect 2026-05-18 sign-off on the staff audit):
  v0.5.0-014 (initial): format_envelope, parse_envelope
  v0.5.0-015 (Stage 1):
    + verify_integrity      — pulled out of core.py (C1)
    + build_envelope_from_record — production sender-side helper (C2)
  v0.6+ (Stage 2, post-016/017): may add validate_config, max_body_size,
    supports_vector_in_envelope per the audit's "Expanded interface" §.
"""
from abc import ABC, abstractmethod

from .envelope import Envelope


class Connector(ABC):
    """Abstract base for v0.5+ connector substrates.

    Concrete subclasses set the class-level `type` attribute to the
    discriminator that matches a `connector.json` entry's `type` field
    (e.g., `"slack"`, `"gdrive"`, `"teams"`).
    """

    #: Discriminator matching the `type` field in connector.json entries.
    #: Subclasses MUST override.
    type: str = ""

    @abstractmethod
    def format_envelope(self, envelope: Envelope) -> str:
        """Construct a substrate-specific message body from a canonical Envelope.

        Caller is responsible for populating envelope fields per
        docs/v0.5_CONNECTOR_ARCHITECTURE.md §5. For senders that built the
        envelope via `build_envelope_from_record`, all required fields are
        already populated correctly — `format_envelope` is a pure render.

        Returns the body string to pass to the substrate's send-message
        primitive (e.g., for Slack: pass to `slack_send_message`'s
        `message` arg).
        """
        ...

    @abstractmethod
    def parse_envelope(self, body: str) -> Envelope | None:
        """Extract the canonical Envelope dict from a substrate message body.

        Returns the parsed envelope dict, or None if no recognizable
        envelope is present (e.g., the message is a human-authored post
        in the channel, not connector traffic).

        Receivers MUST handle None gracefully — skip the message and
        continue iterating. Failing to extract is normal for shared
        channels where humans also post.

        Does NOT verify integrity — that's `verify_integrity`'s job.
        Parsing only constructs the structured Envelope; integrity is a
        separate downstream step the receive flow performs explicitly.
        """
        ...

    @abstractmethod
    def verify_integrity(self, envelope: Envelope) -> tuple[bool, str | None]:
        """Verify the envelope's `content_hash` matches its `content`.

        Each connector substrate owns its integrity scheme. The Slack
        connector uses `"sha256:" + sha256(content).hexdigest()`. Other
        substrates may use signed envelopes, HMAC-MAC, or different hash
        functions; the choice belongs to the connector, not to storage.

        Returns:
          (True,  None)        — content_hash valid.
          (False, "<detail>")  — content_hash invalid; detail is a short
                                 human-readable explanation suitable for
                                 surfacing via the receive flow's
                                 `integrity_failed` error path.

        Receivers MUST treat `verify_integrity` as a hard failure: do not
        persist memories whose integrity check fails (transport corruption,
        tampering, or wire-format drift).
        """
        ...

    # ── Batch wire format (v0.6+) ──────────────────────────────────────
    # Default implementations thunk through the singular methods so existing
    # connectors (SlackConnector §5.1) work batched as "batch of 1" without
    # any code change. Batch-native connectors (CompactSlackConnector)
    # override both with real implementations.
    #
    # Receivers and senders pick batch vs singular based on per-channel
    # context (e.g., sync.db.channels.member_count). The connector itself
    # is format-pure: it knows how to encode and decode, not when to choose.

    def format_batch(self, envelopes: list[Envelope]) -> str:
        """Construct one substrate body containing N envelopes.

        Default: only N==1 supported via format_envelope. Batch-native
        connectors override with a real batched encoding (e.g., gzip+b64
        for compact Slack).
        """
        if len(envelopes) == 1:
            return self.format_envelope(envelopes[0])
        raise NotImplementedError(
            f"{type(self).__name__} does not support batch format "
            f"(got {len(envelopes)} envelopes; default impl is batch-of-1 only)"
        )

    def parse_batch(self, body: str) -> list[Envelope] | None:
        """Extract N envelopes from one substrate body.

        Default: try singular parse, wrap in a 1-list. Batch-native
        connectors override to detect their own header and decode N>=1.
        Returns None if no recognizable envelope content is present
        (caller skips the message).
        """
        single = self.parse_envelope(body)
        return [single] if single is not None else None

    @abstractmethod
    def build_envelope_from_record(self,
                                   record: dict,
                                   connector_id: str) -> Envelope:
        """Construct a canonical Envelope from a local memory record.

        Inputs:
          record       — the dict returned by `core.export_record(id)`
                         (id, vector, content, content_hash, type, tags,
                         project, importance, groups, connector_ids,
                         origin_node, submitted_at, timestamp, ...).
          connector_id — the connector.json entry id this envelope is
                         being routed through (e.g., `"slack-team-mem"`).
                         Appended to envelope.connector_ids if not already
                         present.

        Returns: a populated Envelope ready to pass to `format_envelope`.

        SENDER-SIDE INVARIANTS this method MUST uphold:
          1. envelope.content_hash is the WIRE-FORMAT hash (substrate-
             specific), NOT the local-canonical hash stored in
             record["content_hash"] (which is a 16-char truncated form
             pre-0.5.0-015). The wire hash is computed FROM record.content
             at envelope-build time.
          2. envelope.connector_ids includes `connector_id` (idempotent;
             union with record["connector_ids"]).
          3. envelope.origin_node is the originating peer's node_name —
             taken verbatim from record["origin_node"]. The exporting
             peer's node_name is set at store time in core.store_memory
             (post-0.5.0-012 fix); senders MUST NOT re-resolve.
          4. envelope.submitted_at is preserved from record (originating
             timestamp), NOT updated to the send time. The substrate
             carries its own delivery timestamp out-of-band; submitted_at
             is the originating-peer clock per the wire spec.
          5. envelope.vector MAY be included for substrates that tolerate
             the body size (Slack §5.4 cap = 38 KB; 768-dim vector ≈ 8 KB
             JSON). Including avoids a re-embed at the receiver.
        """
        ...
