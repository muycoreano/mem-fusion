"""
Envelope — canonical schema for the v0.5+ connector wire format.

The Envelope is the substrate-agnostic record that flows through
connectors. Senders produce it from local memory records; receivers
consume it after parsing. Every field is documented here so connector
authors and skill-orchestration code have one source of truth.

WHY A TypedDict (not a dataclass)
---------------------------------
Architect-acked 2026-05-18: TypedDict preserves dict-compat with existing
call sites (every consumer does `envelope.get(key)`), `total=False`
handles optionals naturally, and migration to dataclass later is trivial
if we want hard typing. Don't introduce dataclass discipline mid-flight.

FIELD STATUS
------------
- "required" fields MUST be populated by every sender; receivers reject
  envelopes missing any of them via `validate_envelope`.
- "optional" fields are populated when the substrate or use case calls for
  them; receivers tolerate absence (default per the rules in the field doc).
- "extension" fields: receivers MUST ignore unknown fields. Forward-
  compatibility primitive — future connectors or envelope_version bumps
  can add fields without breaking existing receivers.
"""
from typing import TypedDict


class Envelope(TypedDict, total=False):
    """v0.5 canonical envelope schema.

    REQUIRED fields (every sender MUST populate; receivers reject if missing):
      envelope_version: int  — currently always 1. Future major bumps reserved
                               for format-breaking changes (signatures, etc).
      content:          str  — the memory body. The bytes that produced
                               content_hash. Receivers MUST recompute and
                               verify equality.
      content_hash:     str  — substrate-specific integrity hash. For Slack:
                               `"sha256:" + sha256(content).hexdigest()`.
                               Verified by Connector.verify_integrity at receive.
      origin_node:      str  — the originating peer's node_name (NOT hostname).
                               Used for loopback prevention and cross-peer
                               attribution. Resolved from Constellation config
                               on the sender; never hostname-fallback'd at this
                               point (per 0.5.0-012).
      submitted_at:     str  — ISO-8601 UTC timestamp at the originating peer.

    OPTIONAL fields (senders may populate; receivers handle absence):
      type:             str           — memory type (decision/fact/preference/
                                        error/code/context/session/...).
                                        Default if absent: "".
      tags:             list[str]     — free-form tags. Default if absent: [].
      project:          str           — project tag. Default if absent: "".
      importance:       int           — 1-5 scale. Default if absent: 3.
      connector_ids:    list[str]     — connectors this memory is eligible for.
                                        Default if absent: [].
      groups:           list[str]     — v0.4 routing tag. Ignored at scope-
                                        determination per v0.5 architecture
                                        §4 Migration. Receivers retain it on
                                        the local payload for legacy reads.
      vector:            list[float]  — 768-dim embedding. Substrate-specific
                                        decision to include or omit. If absent,
                                        receiver re-embeds via Ollama.
      received_at:       str          — local-clock ISO-8601; set by receivers
                                        on store, NOT by senders. Senders MUST
                                        NOT populate (would be ignored anyway).

    EXTENSION fields (forward-compat):
      Any other key is ignored by receivers but MAY be populated by senders
      for substrate-specific metadata (e.g., is_smoke_test markers, sender-
      side debug notes). Future envelope_version bumps may promote extension
      keys to required/optional.
    """
    # — Required —
    envelope_version: int
    content:          str
    content_hash:     str
    origin_node:      str
    submitted_at:     str
    # — Optional —
    type:             str
    tags:             list[str]
    project:          str
    importance:       int
    connector_ids:    list[str]
    groups:           list[str]
    vector:           list[float]
    received_at:      str


#: The currently-supported envelope_version. Receivers seeing a higher
#: major version log a warning and accept; receivers seeing a lower version
#: accept silently (forward-compat).
CURRENT_ENVELOPE_VERSION: int = 1

#: Required field names — used by `validate_envelope` and by connector
#: implementations to bail early on malformed input.
REQUIRED_FIELDS: tuple[str, ...] = (
    "envelope_version",
    "content",
    "content_hash",
    "origin_node",
    "submitted_at",
)


def validate_envelope(envelope: dict) -> list[str]:
    """Return a list of validation errors; empty list = valid.

    Checks:
      - Required fields present.
      - envelope_version is int.
      - content / content_hash / origin_node / submitted_at are str.
      - Optional fields, when present, are the expected type.

    Does NOT verify content_hash integrity — that's the connector's
    `verify_integrity`. Validation here is structural only.
    """
    errors: list[str] = []
    if not isinstance(envelope, dict):
        return [f"envelope is not a dict: got {type(envelope).__name__}"]

    for field in REQUIRED_FIELDS:
        if field not in envelope:
            errors.append(f"missing required field: {field}")

    if "envelope_version" in envelope and not isinstance(envelope["envelope_version"], int):
        errors.append("envelope_version must be int")
    for str_field in ("content", "content_hash", "origin_node", "submitted_at"):
        if str_field in envelope and not isinstance(envelope[str_field], str):
            errors.append(f"{str_field} must be str")

    for list_field in ("tags", "connector_ids", "groups", "vector"):
        if list_field in envelope and not isinstance(envelope[list_field], list):
            errors.append(f"{list_field} must be a list")

    if "importance" in envelope and not isinstance(envelope["importance"], int):
        errors.append("importance must be int")

    return errors
