#!/usr/bin/env python3
"""
Constellation Daemon (v0.3.0)

A standalone, persistent HTTP service that hosts the Constellation MCP server
for a single Mem-Fusion node. Sibling daemon to Mem-Fusion's stdio MCP server.

Architecture (per docs/CONSTELLATION_ARCHITECTURE.md):
  - Constellation runs as a separate process from Mem-Fusion
  - HTTP transport, persistent (launchd-managed in production)
  - Owns its own Qdrant collection (mem_fusion_canonical_memories) — disjoint
    from Mem-Fusion's local collection
  - No privileged access to Mem-Fusion's local memory; cross-process isolation
    enforces the safety property

WP2 SCOPE: scaffold only.
  - /health endpoint
  - Config loading + validation
  - Qdrant collection initialization
  - Log infrastructure

WP3 will implement the 4 MCP tools: memory/put, memory/get, peers, peers/self.

Usage:
  python constellation.py --config /path/to/config.json
"""
import argparse
import json
import logging
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PayloadSchemaType


# ── Constants ──────────────────────────────────────────────────────────────
VERSION       = "0.3.0-alpha"
DAEMON_NAME   = "constellation"
VECTOR_SIZE   = 768  # nomic-embed-text dim; must match Mem-Fusion's local store


# ── Config loading ─────────────────────────────────────────────────────────
def load_config(path: Path) -> dict:
    """Load and validate the daemon's config. Raises ValueError on invalid schema."""
    with open(path) as f:
        cfg = json.load(f)

    if "node_name" not in cfg:
        raise ValueError("config missing required field: node_name")
    if "memberships" not in cfg or not isinstance(cfg["memberships"], list):
        raise ValueError("config missing or invalid 'memberships' (must be a list)")

    for i, m in enumerate(cfg["memberships"]):
        for k in ("group_name", "role", "swarm_key"):
            if k not in m:
                raise ValueError(f"memberships[{i}] missing field: {k}")
        if m["role"] not in ("orchestrator", "peer"):
            raise ValueError(f"memberships[{i}].role must be 'orchestrator' or 'peer', got {m['role']!r}")

    # v0.3.0 scope check: enforce single membership
    if len(cfg["memberships"]) > 1:
        raise ValueError(
            "v0.3.0 supports only one membership per node; "
            "multi-membership is reserved for v0.4+"
        )

    # Defaults
    cfg.setdefault("listen_address", "127.0.0.1:7433")
    cfg.setdefault("qdrant_url", "http://127.0.0.1:6333")
    cfg.setdefault("canonical_collection", "mem_fusion_canonical_memories")
    cfg.setdefault("state_dir", str(Path.home() / ".local/share/mem-fusion/constellation"))

    return cfg


# ── Qdrant collection ─────────────────────────────────────────────────────
def init_canonical_collection(qdrant_url: str, collection_name: str, log: logging.Logger):
    """Initialize the canonical Qdrant collection with payload indexes if needed."""
    client = QdrantClient(url=qdrant_url, timeout=10)
    existing = [c.name for c in client.get_collections().collections]

    if collection_name in existing:
        log.info("Canonical collection '%s' already exists", collection_name)
    else:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        log.info("Created canonical collection '%s' (vector_size=%d)", collection_name, VECTOR_SIZE)

    # Payload indexes — needed for v0.3.0 dedup and queries
    indexes = {
        "content_hash": PayloadSchemaType.KEYWORD,
        "group_name":   PayloadSchemaType.KEYWORD,
        "type":         PayloadSchemaType.KEYWORD,
        "tags":         PayloadSchemaType.KEYWORD,
        "origin_node":  PayloadSchemaType.KEYWORD,
        "importance":   PayloadSchemaType.INTEGER,
        "received_at":  PayloadSchemaType.DATETIME,
    }
    for field, schema in indexes.items():
        try:
            client.create_payload_index(collection_name, field, schema)
            log.info("  index: %s (%s)", field, schema.value)
        except Exception as e:
            if "already exists" in str(e).lower():
                continue
            log.warning("  index %s FAILED: %s", field, e)

    return client


# ── HTTP handler ───────────────────────────────────────────────────────────
class ConstellationHandler(BaseHTTPRequestHandler):
    """HTTP request handler. State injected via class attribute before serving."""
    daemon_state: dict = None  # populated at boot

    def log_message(self, fmt: str, *args):
        # Funnel default access log to our logger instead of stderr
        log = self.daemon_state["log"]
        log.info("HTTP %s - %s", self.client_address[0], fmt % args)

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            cfg = self.daemon_state["config"]
            self._send_json(200, {
                "ok":          True,
                "daemon":      DAEMON_NAME,
                "version":     VERSION,
                "node_name":   cfg["node_name"],
                "memberships": [
                    {"group_name": m["group_name"], "role": m["role"]}
                    for m in cfg["memberships"]
                ],
                "scope":       "WP2 scaffold — MCP tools not yet implemented",
            })
        else:
            self._send_json(404, {"error": "not found", "path": self.path})

    def do_POST(self):
        # WP3 will implement MCP-over-HTTP here
        self._send_json(501, {
            "error": "not yet implemented",
            "note":  "MCP tools (memory/put, memory/get, peers, peers/self) land in WP3",
        })


# ── Main ───────────────────────────────────────────────────────────────────
def setup_logging(state_dir: Path) -> logging.Logger:
    log_dir = state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "daemon.log"

    log = logging.getLogger(DAEMON_NAME)
    log.setLevel(logging.INFO)
    # Avoid adding duplicate handlers on re-init
    if not log.handlers:
        fh = logging.FileHandler(str(log_path))
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(fh)
    return log


def main():
    parser = argparse.ArgumentParser(description="Constellation daemon (v0.3.0)")
    parser.add_argument("--config", required=True, help="Path to config.json")
    parser.add_argument("--foreground", action="store_true",
                        help="Run in foreground with console output (for dev/test)")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        cfg = load_config(config_path)
    except ValueError as e:
        print(f"ERROR: invalid config: {e}", file=sys.stderr)
        sys.exit(1)

    state_dir = Path(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(state_dir)
    log.info("=" * 60)
    log.info("Constellation daemon v%s starting", VERSION)
    log.info("  node_name:   %s", cfg["node_name"])
    log.info("  config:      %s", config_path)
    log.info("  state_dir:   %s", state_dir)
    log.info("  qdrant_url:  %s", cfg["qdrant_url"])
    log.info("  collection:  %s", cfg["canonical_collection"])

    for m in cfg["memberships"]:
        log.info("  membership:  %s (role=%s)", m["group_name"], m["role"])

    # Initialize canonical Qdrant collection
    init_canonical_collection(cfg["qdrant_url"], cfg["canonical_collection"], log)

    # Bind HTTP listener
    host, port_str = cfg["listen_address"].split(":")
    port = int(port_str)
    ConstellationHandler.daemon_state = {"config": cfg, "log": log}

    # SO_REUSEADDR — allow rebinding after a quick restart even if the prior
    # socket is in TIME_WAIT. Standard practice for long-lived daemons.
    HTTPServer.allow_reuse_address = True

    try:
        server = HTTPServer((host, port), ConstellationHandler)
    except OSError as e:
        log.error("Failed to bind %s: %s", cfg["listen_address"], e)
        print(f"ERROR: cannot bind {cfg['listen_address']}: {e}", file=sys.stderr)
        sys.exit(1)

    log.info("HTTP listener bound on %s", cfg["listen_address"])
    if args.foreground:
        print(f"✓ Constellation daemon v{VERSION} listening on http://{cfg['listen_address']}")
        print(f"  Node:        {cfg['node_name']}")
        print(f"  Memberships: {[m['group_name'] for m in cfg['memberships']]}")
        print(f"  Qdrant:      {cfg['qdrant_url']} ({cfg['canonical_collection']})")
        print(f"  Log:         {state_dir / 'logs/daemon.log'}")
        print(f"  Try:         curl http://{cfg['listen_address']}/health")
        print(f"  Stop:        Ctrl-C")

    # Graceful shutdown on SIGTERM (for launchd) and SIGINT (for Ctrl-C)
    def _shutdown(signum, _frame):
        log.info("Received signal %d, shutting down", signum)
        server.shutdown()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Daemon shutting down (KeyboardInterrupt)")
    finally:
        server.server_close()
        log.info("Constellation daemon stopped")


if __name__ == "__main__":
    main()
