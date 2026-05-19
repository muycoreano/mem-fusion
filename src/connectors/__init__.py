"""
mem-fusion connectors — substrate adapters.

The Connector interface (in `base.py`) is the wire-format contract every
substrate adapter implements. v0.5 ships SlackConnector; v0.6+ adds
GDriveConnector, TeamsConnector, DiscordConnector, NotionConnector, etc.

ADDING A NEW CONNECTOR
----------------------
1. Create `src/connectors/<substrate>.py` with a class that inherits
   from `Connector` and implements `format_envelope` + `parse_envelope`.
2. Set the `type` class attribute to the discriminator that matches
   `connector.json` entries' `type` field.
3. Register in `_REGISTRY` below.
4. Add tests under `tests/mem_fusion/test-<substrate>-connector.py`.
5. Update `docs/v0.5_CONNECTOR_ARCHITECTURE.md` §3 (type table) and
   §5 (substrate wire format).

That's it — core.py and the skill orchestration are unchanged because
they consume connectors via `get_connector()` at runtime.

USAGE
-----
    from connectors import get_connector
    conn = get_connector("slack")
    body = conn.format_envelope(envelope_dict)
    # ... pass body to slack_send_message via MCP ...
    # ... later, on the pull side: ...
    parsed = conn.parse_envelope(raw_body_from_slack_read_channel)
"""
from .base import Connector
from .compact_slack import CompactSlackConnector
from .slack import SlackConnector

#: Type discriminator → connector class. Register new substrates here.
_REGISTRY: dict[str, type[Connector]] = {
    SlackConnector.type:        SlackConnector,         # "slack"          (§5.1 JSON)
    CompactSlackConnector.type: CompactSlackConnector,  # "slack-compact"  (v0.6 batched)
    # Future:
    # "gdrive":  GDriveConnector,
    # "teams":   TeamsConnector,
    # "discord": DiscordConnector,
    # "notion":  NotionConnector,
}


def get_connector(connector_type: str) -> Connector:
    """Instantiate a connector by its `type` discriminator.

    The `type` value matches `connector.json` entries' `type` field
    (e.g., `"slack"`, `"gdrive"`).

    Raises ValueError on unknown type — caller surfaces the list of
    supported types to the user.
    """
    cls = _REGISTRY.get(connector_type)
    if cls is None:
        supported = ", ".join(sorted(_REGISTRY.keys()))
        raise ValueError(
            f"Unknown connector type {connector_type!r}; supported: {supported}"
        )
    return cls()


def list_connector_types() -> list[str]:
    """Return the sorted list of registered connector types."""
    return sorted(_REGISTRY.keys())


__all__ = [
    "Connector",
    "SlackConnector",
    "CompactSlackConnector",
    "get_connector",
    "list_connector_types",
]
