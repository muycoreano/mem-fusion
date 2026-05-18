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
"""
import json

from .base import Connector


class SlackConnector(Connector):
    """Connector for Slack channels per v0.5 §5.1."""

    type = "slack"

    def format_envelope(self, envelope: dict) -> str:
        """Construct a Slack message body from a canonical envelope.

        Sender-side always emits the `json` language hint and inside-fence
        newlines (more readable in raw paste). Slack normalizes on ingest;
        JSON content round-trips byte-exact through `parse_envelope`.

        Caller is responsible for:
          - Populating `envelope["content"]` (required).
          - Populating `envelope["content_hash"]` per wire format:
            `"sha256:" + sha256(content).hexdigest()`.
          - Populating other envelope fields per §5.1.
          - Checking body size against §5.4 push-side cap (38 KB practical;
            40 KB Slack ceiling). This function does not enforce.
        """
        content  = envelope.get("content", "")
        env_json = json.dumps(envelope, indent=2, ensure_ascii=False)
        return f"{content}\n\n```json\n{env_json}\n```"

    def parse_envelope(self, body: str) -> dict | None:
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
            return json.loads(envelope_text)
        except json.JSONDecodeError:
            return None
