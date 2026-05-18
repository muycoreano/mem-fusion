"""
Connector ABC — interface every connector substrate implements.

A Connector is the WIRE-FORMAT ADAPTER for a productivity-tool substrate
(Slack, GDrive, Teams, Discord, Notion, etc.). It is responsible for ONE
thing: translating between the canonical envelope dict
(per docs/v0.5_CONNECTOR_ARCHITECTURE.md §5) and the substrate's
message body format.

It does NOT:
  - Make API calls (those happen in skill orchestration via Claude's MCP).
  - Maintain cursors or per-connector state (that's core.get/set_connector_cursor).
  - Persist memories (that's core.store_memory_from_envelope, which takes
    the parsed envelope and is substrate-agnostic).

This narrow contract is what lets v0.5 ship one connector cleanly and
v0.6+ add more without core.py churn. core.py never imports specific
connector modules — it imports the package-level get_connector() registry
and asks for whichever type is needed at runtime.
"""
from abc import ABC, abstractmethod


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
    def format_envelope(self, envelope: dict) -> str:
        """Construct a substrate-specific message body from a canonical envelope.

        Caller populates envelope fields per docs/v0.5_CONNECTOR_ARCHITECTURE.md §5
        (content, content_hash, origin_node, submitted_at, type, tags,
        project, importance, connector_ids, optional vector, etc.).

        Returns the body string to pass to the substrate's send-message
        primitive (e.g., for Slack: pass to `slack_send_message`'s
        `message` arg).
        """
        ...

    @abstractmethod
    def parse_envelope(self, body: str) -> dict | None:
        """Extract the canonical envelope dict from a substrate message body.

        Returns the parsed envelope dict, or None if no recognizable
        envelope is present (e.g., the message is a human-authored post
        in the channel, not connector traffic).

        Receivers MUST handle None gracefully — skip the message and
        continue iterating. Failing to extract is normal for shared
        channels where humans also post.
        """
        ...
