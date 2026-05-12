#!/usr/bin/env python3
"""
Constellation Daemon — HTTP peer service for a single Mem-Fusion node.

Sibling daemon to mem_fusion.py. Both are thin proxies over core.py; Qdrant
is the rendezvous between them. Constellation owns the public HTTP surface;
mem-fusion owns the private stdio surface to Claude.

Storage model:
  - One Qdrant collection per peer (shared with mem-fusion via core).
  - Memories originated locally:    source="local"
  - Memories received from peers:   source="federation" (carries origin_node,
                                                          group_name, received_at)
  - dedup key:                       content_hash (byte-identical across daemons)

HTTP surface (MVP — no SSE yet):
  GET  /health                            — liveness
  POST /memory/put                        — accept a memory from a peer
  GET  /memory/get?group_name=...&id=...  — fetch federation entries
  GET  /peers?group_name=...              — directory inferred from federation
                                            entries' origin_node
  GET  /peers/self                        — this node's identity + memberships

Usage:
  python constellation.py --config /path/to/config.json [--foreground]
"""
import argparse
import json
import signal
import sys
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from qdrant_client.models import (
    Distance, FieldCondition, Filter, MatchValue,
    PayloadSchemaType, PointStruct, VectorParams,
)

import core


# ── Constants ──────────────────────────────────────────────────────────────
VERSION          = "0.3.0-alpha"
DAEMON_NAME      = "constellation"
SCROLL_PAGE_SIZE = 256

HTTP_OK          = 200
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN   = 403
HTTP_NOT_FOUND   = 404


# ── Scroll helper ──────────────────────────────────────────────────────────
def scroll_all(collection: str, scroll_filter, payload_keys, page_size: int = SCROLL_PAGE_SIZE):
    """Yield every Qdrant point matching `scroll_filter`, paginating internally.

    Cursor-based pagination is do-while in nature: the first request has no
    offset, and we only know if there's a next page from the response.
    Bootstrap explicitly so the main loop carries a clean termination condition.
    """
    def _page(offset):
        return core.qdrant.scroll(
            collection_name=collection,
            scroll_filter=scroll_filter,
            limit=page_size,
            with_payload=payload_keys,
            with_vectors=False,
            offset=offset,
        )

    points, next_offset = _page(None)
    yield from points
    while next_offset is not None:
        points, next_offset = _page(next_offset)
        yield from points


def point_to_record(p) -> dict:
    """Wire shape for a federation entry returned by /memory/get."""
    pl = p.payload or {}
    return {
        "canonical_id":         str(p.id),
        "vector":               list(p.vector) if p.vector is not None else None,
        "content":              pl.get("content", ""),
        "content_hash":         pl.get("content_hash", ""),
        "type":                 pl.get("type", ""),
        "tags":                 pl.get("tags", []),
        "importance":           pl.get("importance", 3),
        "original_timestamp":   pl.get("original_timestamp", ""),
        "group_name":           pl.get("group_name", ""),
        "origin_node":          pl.get("origin_node", ""),
        "origin_local_id":      pl.get("origin_local_id", ""),
        "submitted_at":         pl.get("submitted_at", ""),
        "received_at":          pl.get("received_at", ""),
        "submission_kind":      pl.get("submission_kind", ""),
    }


# ── Config loading ─────────────────────────────────────────────────────────
def load_config(path: Path) -> dict:
    """Load and validate the daemon's config. Raises ValueError on invalid schema.

    The Qdrant collection is fixed at `core.COLLECTION` — it is never a config
    field. Isolation between peers comes from distinct Qdrant ports, not names.
    """
    with open(path) as f:
        cfg = json.load(f)

    if "node_name" not in cfg:
        raise ValueError("config missing required field: node_name")
    if "memberships" not in cfg or not isinstance(cfg["memberships"], list):
        raise ValueError("config missing or invalid 'memberships' (must be a list)")

    for i, m in enumerate(cfg["memberships"]):
        for k in ("group_name", "role"):
            if k not in m:
                raise ValueError(f"memberships[{i}] missing field: {k}")
        if m["role"] not in ("orchestrator", "peer"):
            raise ValueError(f"memberships[{i}].role must be 'orchestrator' or 'peer', got {m['role']!r}")

    if len(cfg["memberships"]) > 1:
        raise ValueError(
            "v0.3.0 supports only one membership per node; "
            "multi-membership is reserved for post-MVP"
        )

    cfg.setdefault("listen_address", "127.0.0.1:7433")
    cfg.setdefault("qdrant_url", core.QDRANT_URL)
    cfg.setdefault("state_dir", str(Path.home() / ".local/share/mem-fusion/constellation"))

    return cfg


# ── Qdrant collection ─────────────────────────────────────────────────────
def init_collection(collection_name: str, log):
    """Ensure the shared collection exists with the payload indexes we need.

    Same collection as mem-fusion. Adding indexes is idempotent; the federation
    payload fields (origin_node, group_name, received_at) get their own indexes
    so /peers and /memory/get scrolls stay efficient.
    """
    existing = [c.name for c in core.qdrant.get_collections().collections]
    if collection_name in existing:
        log.info("Collection '%s' already exists", collection_name)
    else:
        core.qdrant.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=core.VECTOR_SIZE, distance=Distance.COSINE),
        )
        log.info("Created collection '%s' (vector_size=%d)", collection_name, core.VECTOR_SIZE)

    indexes = {
        "content_hash": PayloadSchemaType.KEYWORD,
        "group_name":   PayloadSchemaType.KEYWORD,
        "source":       PayloadSchemaType.KEYWORD,
        "type":         PayloadSchemaType.KEYWORD,
        "tags":         PayloadSchemaType.KEYWORD,
        "origin_node":  PayloadSchemaType.KEYWORD,
        "importance":   PayloadSchemaType.INTEGER,
        "timestamp":    PayloadSchemaType.DATETIME,
        "received_at":  PayloadSchemaType.DATETIME,
    }
    for field, schema in indexes.items():
        try:
            core.qdrant.create_payload_index(collection_name, field, schema)
            log.info("  index: %s (%s)", field, schema.value)
        except Exception as e:
            if "already exists" in str(e).lower():
                continue
            log.warning("  index %s FAILED: %s", field, e)


# ── HTTP handler ───────────────────────────────────────────────────────────
class ConstellationHandler(BaseHTTPRequestHandler):
    """HTTP request handler. State injected via class attribute before serving."""
    daemon_state: dict = None  # populated at boot

    def log_message(self, fmt: str, *args):
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
            self._send_json(HTTP_OK, {
                "ok":          True,
                "daemon":      DAEMON_NAME,
                "version":     VERSION,
                "node_name":   cfg["node_name"],
                "memberships": [
                    {"group_name": m["group_name"], "role": m["role"]}
                    for m in cfg["memberships"]
                ],
            })
        elif self.path.startswith("/memory/get"):
            self._handle_memory_get()
        elif self.path.startswith("/peers/self"):
            self._handle_peers_self()
        elif self.path.startswith("/peers"):
            self._handle_peers_list()
        else:
            self._send_json(HTTP_NOT_FOUND, {"error": "not found", "path": self.path})

    def do_POST(self):
        if self.path == "/memory/put":
            self._handle_memory_put()
        else:
            self._send_json(HTTP_NOT_FOUND, {"error": "not found", "path": self.path})

    # ── /memory/put — receive a peer's memory into this node's collection ──
    def _handle_memory_put(self):
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]
        collection = core.COLLECTION

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return self._send_json(HTTP_BAD_REQUEST, {"error": "empty body"})
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as e:
            return self._send_json(HTTP_BAD_REQUEST, {"error": f"invalid JSON: {e}"})

        for k in ("group_name", "memory_record", "provenance"):
            if k not in body:
                return self._send_json(HTTP_BAD_REQUEST, {"error": f"missing top-level field: {k}"})

        group_name = body["group_name"]
        record     = body["memory_record"]
        provenance = body["provenance"]

        orchestrated = [m["group_name"] for m in cfg["memberships"] if m["role"] == "orchestrator"]
        if group_name not in orchestrated:
            return self._send_json(HTTP_FORBIDDEN, {
                "error":            f"this node does not orchestrate {group_name!r}",
                "orchestrated":     orchestrated,
            })

        required = ("content", "vector", "content_hash",
                    "type", "importance", "original_timestamp")
        for k in required:
            if k not in record:
                return self._send_json(HTTP_BAD_REQUEST, {"error": f"memory_record missing field: {k}"})

        expected_chash = core.content_hash(record["content"])
        if expected_chash != record["content_hash"]:
            return self._send_json(HTTP_BAD_REQUEST, {
                "error":              "content_hash mismatch",
                "expected":           expected_chash,
                "received":           record["content_hash"],
                "note":               "content was modified somewhere in transit",
            })

        vec = record["vector"]
        if not isinstance(vec, list) or len(vec) != core.VECTOR_SIZE:
            return self._send_json(HTTP_BAD_REQUEST, {
                "error":              f"vector must be {core.VECTOR_SIZE}-dim list",
                "received_length":    len(vec) if isinstance(vec, list) else None,
                "received_type":      type(vec).__name__,
            })

        # Dedup on content_hash + group_name + source=federation.
        # Locally-originated rows with the same hash are not duplicates from a
        # federation perspective; the peer asked us to record an inbound copy.
        existing, _ = core.qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[
                FieldCondition(key="content_hash", match=MatchValue(value=record["content_hash"])),
                FieldCondition(key="group_name",   match=MatchValue(value=group_name)),
                FieldCondition(key="source",       match=MatchValue(value="federation")),
            ]),
            limit=1, with_payload=False,
        )
        if existing:
            existing_id = str(existing[0].id)
            log.info("PUT duplicate: group=%s hash=%s existing=%s",
                     group_name, record["content_hash"], existing_id)
            return self._send_json(HTTP_OK, {
                "status":        "duplicate",
                "canonical_id":  existing_id,
            })

        canonical_id = str(uuid.uuid4())
        received_at  = core.iso_now()
        payload = {
            "content":            record["content"],
            "content_hash":       record["content_hash"],
            "type":               record["type"],
            "tags":               record.get("tags", []),
            "importance":         record["importance"],
            "original_timestamp": record["original_timestamp"],
            "group_name":         group_name,
            "source":             "federation",
            "origin_node":        provenance.get("origin_node", ""),
            "origin_local_id":    provenance.get("origin_local_id", ""),
            "submitted_at":       provenance.get("submitted_at", ""),
            "submission_kind":    provenance.get("submission_kind", ""),
            "received_at":        received_at,
            "received_by":        cfg["node_name"],
            # `timestamp` mirrors `received_at` so mem-fusion's `search_recent`
            # (which filters on `timestamp`) surfaces federation entries naturally.
            "timestamp":          received_at,
        }
        core.qdrant.upsert(
            collection_name=collection,
            points=[PointStruct(id=canonical_id, vector=vec, payload=payload)],
        )
        log.info("PUT stored: group=%s id=%s origin=%s/%s",
                 group_name, canonical_id,
                 provenance.get("origin_node", "?"),
                 (provenance.get("origin_local_id", "?") or "?")[:8])

        return self._send_json(HTTP_OK, {
            "status":        "stored",
            "canonical_id":  canonical_id,
            "received_at":   received_at,
        })

    # ── /memory/get — fetch federation entries from this node ─────────────
    def _handle_memory_get(self):
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]
        collection = core.COLLECTION

        params = parse_qs(urlparse(self.path).query)
        group_name = (params.get("group_name") or [None])[0]
        memory_id  = (params.get("id") or [None])[0]
        limit      = int((params.get("limit") or ["10"])[0])

        if not group_name:
            return self._send_json(HTTP_BAD_REQUEST, {"error": "missing query param: group_name"})

        orchestrated = [m["group_name"] for m in cfg["memberships"] if m["role"] == "orchestrator"]
        if group_name not in orchestrated:
            return self._send_json(HTTP_FORBIDDEN, {
                "error":         f"this node does not orchestrate {group_name!r}",
                "orchestrated":  orchestrated,
            })

        if memory_id:
            points = core.qdrant.retrieve(
                collection_name=collection,
                ids=[memory_id], with_vectors=True, with_payload=True,
            )
            if not points:
                return self._send_json(HTTP_NOT_FOUND, {"error": f"memory {memory_id} not found"})
            p = points[0]
            pl = p.payload or {}
            if pl.get("group_name") != group_name or pl.get("source") != "federation":
                return self._send_json(HTTP_NOT_FOUND, {
                    "error":     f"memory {memory_id} not a federation entry in group {group_name!r}",
                })
            log.info("GET by-id: group=%s id=%s", group_name, memory_id)
            return self._send_json(HTTP_OK, {"count": 1, "memories": [point_to_record(p)]})

        points, _ = core.qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[
                FieldCondition(key="group_name", match=MatchValue(value=group_name)),
                FieldCondition(key="source",     match=MatchValue(value="federation")),
            ]),
            limit=limit, with_payload=True, with_vectors=True,
        )
        log.info("GET scroll: group=%s count=%d", group_name, len(points))
        return self._send_json(HTTP_OK, {
            "count":    len(points),
            "memories": [point_to_record(p) for p in points],
        })

    # ── /peers — directory inferred from federation entries' origin_node ──
    def _handle_peers_list(self):
        """Aggregate origin_node stats from federation entries in this group.

        v0.3.0 has no registration step — peers are inferred from submission
        activity. A node that has never PUT is not listed. Adequate for the
        trust-as-membership model where visibility follows interaction.
        """
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]
        collection = core.COLLECTION

        params = parse_qs(urlparse(self.path).query)
        group_name = (params.get("group_name") or [None])[0]
        if not group_name:
            return self._send_json(HTTP_BAD_REQUEST, {"error": "missing query param: group_name"})

        orchestrated = [m["group_name"] for m in cfg["memberships"] if m["role"] == "orchestrator"]
        if group_name not in orchestrated:
            return self._send_json(HTTP_FORBIDDEN, {
                "error":        f"this node does not orchestrate {group_name!r}",
                "orchestrated": orchestrated,
            })

        group_filter = Filter(must=[
            FieldCondition(key="group_name", match=MatchValue(value=group_name)),
            FieldCondition(key="source",     match=MatchValue(value="federation")),
        ])
        peers: dict = {}
        for p in scroll_all(collection, group_filter,
                            payload_keys=["origin_node", "received_at"]):
            origin = (p.payload or {}).get("origin_node") or ""
            ts     = (p.payload or {}).get("received_at") or ""
            if not origin:
                continue
            entry = peers.setdefault(origin, {
                "node_name":         origin,
                "submission_count":  0,
                "first_seen":        ts,
                "last_seen":         ts,
            })
            entry["submission_count"] += 1
            if ts and ts < entry["first_seen"]:
                entry["first_seen"] = ts
            if ts and ts > entry["last_seen"]:
                entry["last_seen"] = ts

        peer_list = sorted(peers.values(), key=lambda e: e["last_seen"], reverse=True)
        log.info("GET /peers: group=%s count=%d", group_name, len(peer_list))
        return self._send_json(HTTP_OK, {
            "group_name":   group_name,
            "orchestrator": cfg["node_name"],
            "count":        len(peer_list),
            "peers":        peer_list,
        })

    # ── /peers/self — this node's identity and group memberships ─────────
    def _handle_peers_self(self):
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]
        log.info("GET /peers/self")
        return self._send_json(HTTP_OK, {
            "node_name":      cfg["node_name"],
            "listen_address": cfg["listen_address"],
            "version":        VERSION,
            "memberships": [
                {"group_name": m["group_name"], "role": m["role"]}
                for m in cfg["memberships"]
            ],
        })


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Constellation daemon")
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

    # Point core's module-level Qdrant client at the daemon's configured URL.
    # (Dev peers run their own Qdrant on a distinct port; production uses the
    # default 6333.) Collection name is invariant — never rewritten.
    core.QDRANT_URL = cfg["qdrant_url"]
    from qdrant_client import QdrantClient
    core.qdrant = QdrantClient(url=cfg["qdrant_url"], timeout=10)

    log = core.configure_logging(DAEMON_NAME)
    log.info("=" * 60)
    log.info("Constellation daemon v%s starting", VERSION)
    log.info("  node_name:   %s", cfg["node_name"])
    log.info("  config:      %s", config_path)
    log.info("  state_dir:   %s", state_dir)
    log.info("  qdrant_url:  %s", cfg["qdrant_url"])
    log.info("  collection:  %s", core.COLLECTION)
    for m in cfg["memberships"]:
        log.info("  membership:  %s (role=%s)", m["group_name"], m["role"])

    init_collection(core.COLLECTION, log)

    host, port_str = cfg["listen_address"].split(":")
    port = int(port_str)
    ConstellationHandler.daemon_state = {"config": cfg, "log": log}

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
        print(f"  Qdrant:      {cfg['qdrant_url']} ({core.COLLECTION})")
        print(f"  Log:         {core.ACTIVE_LOG_PATH}")
        print(f"  Try:         curl http://{cfg['listen_address']}/health")
        print(f"  Stop:        Ctrl-C")

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
