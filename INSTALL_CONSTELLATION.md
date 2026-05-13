# Constellation — Setup Prompt for Claude Code

> **How to use this file:** Open Claude Code in a terminal on your Mac and paste this entire document as your first message. Claude will read it, run the steps, and verify everything works. **Mem-Fusion must already be installed first** — Constellation reuses Mem-Fusion's Qdrant and Python venv.

---

## What this builds

A persistent HTTP daemon that lets Mem-Fusion peers share memory with each other. Constellation is a backend for the local Mem-Fusion MCP server — Claude never talks to Constellation directly; Mem-Fusion does, through a localhost-only gateway.

- **Peer surface** on port 7533 (bound to all interfaces) — other peers' Constellation daemons reach this one for memory delivery and catch-up.
- **Gateway surface** on port 7534 (bound to 127.0.0.1) — what Mem-Fusion calls when Claude invokes `group_pull` or `group_push`.
- **Shared Qdrant collection** (`cowork_memories`) — group entries carry `source=group`, `origin_node`, `group_name`, `submitted_at` payload tags.
- **launchd service** (`com.branchapp.memfusion.constellation`) — KeepAlive, RunAtLoad.

Disk footprint: minimal (<10 MB beyond what Mem-Fusion already uses).

---

## Pre-flight — verify before starting

```bash
# 1. macOS only
[[ "$(uname)" == "Darwin" ]] || { echo "This installer is macOS-only"; exit 1; }

# 2. Mem-Fusion already installed
[[ -f ~/.local/share/mem-fusion/mem_fusion.py ]] || \
  { echo "Install Mem-Fusion first — see INSTALL_MEM_FUSION.md"; exit 1; }
[[ -x ~/.local/share/mem-fusion/venv/bin/python ]] || \
  { echo "Mem-Fusion venv not found; reinstall Mem-Fusion first"; exit 1; }

# 3. Mem-Fusion services running
curl -s http://127.0.0.1:6333/healthz   >/dev/null || { echo "Qdrant not up — check Mem-Fusion install"; exit 1; }
curl -s http://127.0.0.1:11434/api/tags >/dev/null || { echo "Ollama not up — check Mem-Fusion install"; exit 1; }

# 4. Ports 7533 and 7534 free
lsof -nP -iTCP:7533 -sTCP:LISTEN -t >/dev/null 2>&1 && \
  { echo "Port 7533 already in use — stop the conflicting process first"; exit 1; }
lsof -nP -iTCP:7534 -sTCP:LISTEN -t >/dev/null 2>&1 && \
  { echo "Port 7534 already in use — stop the conflicting process first"; exit 1; }
```

If any check fails, **stop and ask the user** before continuing.

---

## Step 1 — Lay out the Constellation directory

```bash
mkdir -p ~/.local/share/mem-fusion/constellation/{logs,state}
```

---

## Step 2 — Install the Constellation daemon

```bash
cat > ~/.local/share/mem-fusion/constellation.py <<'C088079119A2_EOF'
#!/usr/bin/env python3
"""
Constellation Daemon — group memory backend for a single Mem-Fusion node.

Two HTTP listeners in one process:

  PEER surface — bound to peer_listen_address (default 0.0.0.0:7533).
    What other peers in the group call to share memories with this peer.
      GET  /health                    — liveness
      GET  /peers/self                — this node's identity + memberships
      GET  /peers?group_name=         — directory inferred from received entries
      POST /memory/put                — receive a memory from another peer
      GET  /memory/get?group_name=    — fetch group entries by id or scroll
      GET  /memory/since?group_name=&cursor=  — pull catch-up since cursor

  GATEWAY surface — bound to gateway_listen_address (default 127.0.0.1:7534).
    What the local mem-fusion process calls to drive group operations.
    Localhost-only by network binding.
      POST /pull   — pull new memories from every peer in the group
      POST /push   — share a local memory with every peer in the group

Storage model (shared Qdrant collection `cowork_memories`):
  source="local"  — written by Mem-Fusion on this peer. If the peer pushes the
                    memory to a group, the local entry is augmented in place
                    with group_name, origin_node (= self), submitted_at.
  source="group"  — received from another peer via /memory/put or pull.

Dedup is on (content_hash, group_name) regardless of source — a peer that
echoes back its own originated memory via pull doesn't create a duplicate.

Usage:
  python constellation.py --config /path/to/config.json [--foreground]
"""
import argparse
import json
import signal
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from qdrant_client.models import (
    Distance, FieldCondition, Filter, MatchValue,
    PayloadSchemaType, PointStruct, VectorParams,
)

import core


VERSION          = "0.3.0-alpha"
DAEMON_NAME      = "constellation"
SCROLL_PAGE_SIZE = 256
PEER_HTTP_TIMEOUT = 10.0
PULL_LIMIT_DEFAULT = 256

HTTP_OK          = 200
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN   = 403
HTTP_NOT_FOUND   = 404
HTTP_INTERNAL    = 500


# ── Scroll helper ──────────────────────────────────────────────────────────
def scroll_all(collection, scroll_filter, payload_keys, page_size=SCROLL_PAGE_SIZE):
    """Yield every Qdrant point matching `scroll_filter`, paginating internally."""
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


def point_to_record(p):
    """Wire shape for a group entry returned by peer-side /memory/get."""
    pl = p.payload or {}
    return {
        "id":           str(p.id),
        "content":      pl.get("content", ""),
        "content_hash": pl.get("content_hash", ""),
        "vector":       list(p.vector) if p.vector is not None else None,
        "type":         pl.get("type", ""),
        "tags":         pl.get("tags", []),
        "project":      pl.get("project", ""),
        "importance":   pl.get("importance", 3),
        "group_name":   pl.get("group_name", ""),
        "origin_node":  pl.get("origin_node", ""),
        "submitted_at": pl.get("submitted_at", ""),
        "received_at":  pl.get("received_at", ""),
    }


# ── Config loading ─────────────────────────────────────────────────────────
def load_config(path):
    """Load and validate the daemon's config.

    Required: node_name, memberships[].group_name, memberships[].peers[].
    Each peer entry: {node_name, endpoint}. v0.3.0 = single membership.
    """
    with open(path) as f:
        cfg = json.load(f)

    if "node_name" not in cfg:
        raise ValueError("config missing required field: node_name")
    if "memberships" not in cfg or not isinstance(cfg["memberships"], list):
        raise ValueError("config missing or invalid 'memberships' (must be a list)")
    if len(cfg["memberships"]) != 1:
        raise ValueError("v0.3.0 supports exactly one membership per node")

    for i, m in enumerate(cfg["memberships"]):
        if "group_name" not in m:
            raise ValueError(f"memberships[{i}] missing field: group_name")
        if "peers" not in m or not isinstance(m["peers"], list):
            raise ValueError(f"memberships[{i}] missing 'peers' list")
        for j, p in enumerate(m["peers"]):
            if "node_name" not in p or "endpoint" not in p:
                raise ValueError(
                    f"memberships[{i}].peers[{j}] needs both 'node_name' and 'endpoint'"
                )

    cfg.setdefault("peer_listen_address",    "0.0.0.0:7533")
    cfg.setdefault("gateway_listen_address", "127.0.0.1:7534")
    cfg.setdefault("qdrant_url",             core.QDRANT_URL)
    cfg.setdefault("state_dir",              str(Path.home() / ".local/share/mem-fusion/constellation"))

    return cfg


# ── Qdrant collection ─────────────────────────────────────────────────────
def init_collection(collection_name, log):
    """Ensure the shared collection + payload indexes exist (idempotent)."""
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
        "submitted_at": PayloadSchemaType.DATETIME,
        "received_at":  PayloadSchemaType.DATETIME,
    }
    for field, schema in indexes.items():
        try:
            core.qdrant.create_payload_index(collection_name, field, schema)
        except Exception as e:
            if "already exists" not in str(e).lower():
                log.warning("  index %s FAILED: %s", field, e)


# ── Shared response helpers ────────────────────────────────────────────────
class _BaseHandler(BaseHTTPRequestHandler):
    daemon_state = None  # populated at boot by main()

    def log_message(self, fmt, *args):
        self.daemon_state["log"].info("HTTP %s - %s", self.client_address[0], fmt % args)

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return None, "empty body"
        try:
            return json.loads(self.rfile.read(length)), None
        except json.JSONDecodeError as e:
            return None, f"invalid JSON: {e}"


# ── Peer-facing HTTP handler ───────────────────────────────────────────────
class PeerHandler(_BaseHandler):
    """Endpoints other peers in the group call to share memories with us."""

    def do_GET(self):
        if self.path == "/health":
            return self._handle_health()
        if self.path.startswith("/memory/get"):
            return self._handle_memory_get()
        if self.path.startswith("/memory/since"):
            return self._handle_memory_since()
        if self.path.startswith("/peers/self"):
            return self._handle_peers_self()
        if self.path.startswith("/peers"):
            return self._handle_peers_list()
        return self._send_json(HTTP_NOT_FOUND, {"error": "not found", "path": self.path})

    def do_POST(self):
        if self.path == "/memory/put":
            return self._handle_memory_put()
        return self._send_json(HTTP_NOT_FOUND, {"error": "not found", "path": self.path})

    # ── /health ───────────────────────────────────────────────────────────
    def _handle_health(self):
        cfg = self.daemon_state["config"]
        return self._send_json(HTTP_OK, {
            "ok":          True,
            "daemon":      DAEMON_NAME,
            "version":     VERSION,
            "node_name":   cfg["node_name"],
            "memberships": [m["group_name"] for m in cfg["memberships"]],
        })

    # ── /memory/put — receive a memory from another peer ─────────────────
    def _handle_memory_put(self):
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]

        body, err = self._read_json()
        if err:
            return self._send_json(HTTP_BAD_REQUEST, {"error": err})

        # Flat record. All fields top-level.
        for k in ("content", "content_hash", "vector", "type", "importance",
                  "group_name", "origin_node", "submitted_at"):
            if k not in body:
                return self._send_json(HTTP_BAD_REQUEST, {"error": f"missing field: {k}"})

        group_name = body["group_name"]
        memberships = [m["group_name"] for m in cfg["memberships"]]
        if group_name not in memberships:
            return self._send_json(HTTP_FORBIDDEN, {
                "error":       f"this node is not a member of {group_name!r}",
                "memberships": memberships,
            })

        if core.content_hash(body["content"]) != body["content_hash"]:
            return self._send_json(HTTP_BAD_REQUEST, {
                "error": "content_hash mismatch — content modified in transit",
            })

        vec = body["vector"]
        if not isinstance(vec, list) or len(vec) != core.VECTOR_SIZE:
            return self._send_json(HTTP_BAD_REQUEST, {
                "error":           f"vector must be {core.VECTOR_SIZE}-dim list",
                "received_length": len(vec) if isinstance(vec, list) else None,
            })

        # Dedup on (content_hash, group_name). Source-agnostic — a peer's own
        # originated memory shouldn't be inserted again as source=group.
        existing, _ = core.qdrant.scroll(
            collection_name=core.COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="content_hash", match=MatchValue(value=body["content_hash"])),
                FieldCondition(key="group_name",   match=MatchValue(value=group_name)),
            ]),
            limit=1, with_payload=False,
        )
        if existing:
            existing_id = str(existing[0].id)
            log.info("PUT duplicate: group=%s hash=%s existing=%s",
                     group_name, body["content_hash"], existing_id)
            return self._send_json(HTTP_OK, {
                "status": "duplicate",
                "id":     existing_id,
            })

        new_id      = str(uuid.uuid4())
        received_at = core.iso_now()
        payload = {
            "content":      body["content"],
            "content_hash": body["content_hash"],
            "type":         body["type"],
            "tags":         body.get("tags", []),
            "project":      body.get("project", ""),
            "importance":   body["importance"],
            "group_name":   group_name,
            "source":       "group",
            "origin_node":  body["origin_node"],
            "submitted_at": body["submitted_at"],
            "received_at":  received_at,
            # `timestamp` mirrors `received_at` so mem-fusion's search_recent
            # surfaces group entries naturally without code paths needing to know.
            "timestamp":    received_at,
        }
        core.qdrant.upsert(
            collection_name=core.COLLECTION,
            points=[PointStruct(id=new_id, vector=vec, payload=payload)],
        )
        log.info("PUT stored: group=%s id=%s origin=%s",
                 group_name, new_id, body["origin_node"])
        return self._send_json(HTTP_OK, {
            "status": "stored",
            "id":     new_id,
        })

    # ── /memory/get ───────────────────────────────────────────────────────
    def _handle_memory_get(self):
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]

        params = parse_qs(urlparse(self.path).query)
        group_name = (params.get("group_name") or [None])[0]
        memory_id  = (params.get("id") or [None])[0]
        limit      = int((params.get("limit") or ["10"])[0])

        if not group_name:
            return self._send_json(HTTP_BAD_REQUEST, {"error": "missing query param: group_name"})

        memberships = [m["group_name"] for m in cfg["memberships"]]
        if group_name not in memberships:
            return self._send_json(HTTP_FORBIDDEN, {
                "error":       f"this node is not a member of {group_name!r}",
                "memberships": memberships,
            })

        if memory_id:
            points = core.qdrant.retrieve(
                collection_name=core.COLLECTION,
                ids=[memory_id], with_vectors=True, with_payload=True,
            )
            if not points or (points[0].payload or {}).get("group_name") != group_name:
                return self._send_json(HTTP_NOT_FOUND, {
                    "error": f"memory {memory_id} not in group {group_name!r}",
                })
            log.info("GET by-id: group=%s id=%s", group_name, memory_id)
            return self._send_json(HTTP_OK, {"count": 1, "memories": [point_to_record(points[0])]})

        points, _ = core.qdrant.scroll(
            collection_name=core.COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="group_name", match=MatchValue(value=group_name)),
            ]),
            limit=limit, with_payload=True, with_vectors=True,
        )
        log.info("GET scroll: group=%s count=%d", group_name, len(points))
        return self._send_json(HTTP_OK, {
            "count":    len(points),
            "memories": [point_to_record(p) for p in points],
        })

    # ── /memory/since — catch-up pull for a peer with a cursor ──────────
    def _handle_memory_since(self):
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]

        params = parse_qs(urlparse(self.path).query)
        group_name = (params.get("group_name") or [None])[0]
        cursor     = (params.get("cursor") or [None])[0]
        limit      = int((params.get("limit") or [str(PULL_LIMIT_DEFAULT)])[0])

        if not group_name:
            return self._send_json(HTTP_BAD_REQUEST, {"error": "missing query param: group_name"})

        memberships = [m["group_name"] for m in cfg["memberships"]]
        if group_name not in memberships:
            return self._send_json(HTTP_FORBIDDEN, {
                "error":       f"this node is not a member of {group_name!r}",
                "memberships": memberships,
            })

        memories = core.get_entries_for_pull(group_name, cursor, limit)
        log.info("GET /memory/since: group=%s cursor=%s returned=%d",
                 group_name, cursor or "-", len(memories))
        return self._send_json(HTTP_OK, {
            "count":    len(memories),
            "memories": memories,
        })

    # ── /peers — directory inferred from group entries' origin_node ──────
    def _handle_peers_list(self):
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]

        params = parse_qs(urlparse(self.path).query)
        group_name = (params.get("group_name") or [None])[0]
        if not group_name:
            return self._send_json(HTTP_BAD_REQUEST, {"error": "missing query param: group_name"})

        memberships = [m["group_name"] for m in cfg["memberships"]]
        if group_name not in memberships:
            return self._send_json(HTTP_FORBIDDEN, {
                "error":       f"this node is not a member of {group_name!r}",
                "memberships": memberships,
            })

        seen = {}
        for p in scroll_all(
            core.COLLECTION,
            Filter(must=[FieldCondition(key="group_name", match=MatchValue(value=group_name))]),
            payload_keys=["origin_node", "received_at"],
        ):
            origin = (p.payload or {}).get("origin_node") or ""
            ts     = (p.payload or {}).get("received_at") or ""
            if not origin or origin == cfg["node_name"]:
                continue
            entry = seen.setdefault(origin, {
                "node_name":        origin,
                "submission_count": 0,
                "first_seen":       ts,
                "last_seen":        ts,
            })
            entry["submission_count"] += 1
            if ts and ts < entry["first_seen"]:
                entry["first_seen"] = ts
            if ts and ts > entry["last_seen"]:
                entry["last_seen"] = ts

        peer_list = sorted(seen.values(), key=lambda e: e["last_seen"], reverse=True)
        log.info("GET /peers: group=%s count=%d", group_name, len(peer_list))
        return self._send_json(HTTP_OK, {
            "group_name":      group_name,
            "responding_node": cfg["node_name"],
            "count":           len(peer_list),
            "peers":           peer_list,
        })

    # ── /peers/self ──────────────────────────────────────────────────────
    def _handle_peers_self(self):
        cfg = self.daemon_state["config"]
        self.daemon_state["log"].info("GET /peers/self")
        return self._send_json(HTTP_OK, {
            "node_name":      cfg["node_name"],
            "listen_address": cfg["peer_listen_address"],
            "version":        VERSION,
            "memberships":    [m["group_name"] for m in cfg["memberships"]],
        })


# ── Gateway HTTP handler (mem-fusion → constellation, localhost-only) ─────
class GatewayHandler(_BaseHandler):
    """Endpoints the local mem-fusion process calls to drive group operations.

    Bound to 127.0.0.1; network-enforced localhost-only.
    """

    def do_POST(self):
        if self.path == "/pull":
            return self._handle_pull()
        if self.path == "/push":
            return self._handle_push()
        return self._send_json(HTTP_NOT_FOUND, {"error": "not found", "path": self.path})

    # ── POST /pull — fan out GET /memory/since to every peer in group ────
    def _handle_pull(self):
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]
        membership = cfg["memberships"][0]
        group_name = membership["group_name"]
        peers      = membership["peers"]

        cursor = core.max_submitted_at_in_group(group_name)
        log.info("PULL start: group=%s cursor=%s peers=%d",
                 group_name, cursor or "-", len(peers))

        results = []
        for peer in peers:
            r = {"node_name": peer["node_name"], "group_name": group_name}
            try:
                params = {"group_name": group_name}
                if cursor:
                    params["cursor"] = cursor
                resp = httpx.get(f"{peer['endpoint']}/memory/since",
                                 params=params, timeout=PEER_HTTP_TIMEOUT)
                if resp.status_code != HTTP_OK:
                    r["status"] = "unreachable"
                    r["reason"] = f"http {resp.status_code}"
                else:
                    inserted = self._insert_pulled(resp.json().get("memories", []), group_name)
                    r["status"]    = "responsive"
                    r["entry_ids"] = inserted
                    log.info("PULL from %s: inserted=%d", peer["node_name"], len(inserted))
            except httpx.ConnectError:
                r["status"], r["reason"] = "unreachable", "connection refused"
            except httpx.TimeoutException:
                r["status"], r["reason"] = "unreachable", "timeout"
            except Exception as e:
                r["status"], r["reason"] = "unreachable", str(e)[:200]
            results.append(r)

        results.sort(key=lambda r: r["node_name"])
        return self._send_json(HTTP_OK, {"peers": results})

    def _insert_pulled(self, memories, group_name):
        """Insert pulled entries into local Qdrant; dedup by (content_hash, group_name)."""
        inserted_ids = []
        for mem in memories:
            existing, _ = core.qdrant.scroll(
                collection_name=core.COLLECTION,
                scroll_filter=Filter(must=[
                    FieldCondition(key="content_hash",
                                   match=MatchValue(value=mem.get("content_hash", ""))),
                    FieldCondition(key="group_name",
                                   match=MatchValue(value=group_name)),
                ]),
                limit=1, with_payload=False,
            )
            if existing:
                continue
            canonical_id = str(uuid.uuid4())
            received_at = core.iso_now()
            payload = {
                "content":      mem.get("content", ""),
                "content_hash": mem.get("content_hash", ""),
                "type":         mem.get("type", ""),
                "tags":         mem.get("tags", []),
                "project":      mem.get("project", ""),
                "importance":   mem.get("importance", 3),
                "group_name":   group_name,
                "source":       "group",
                "origin_node":  mem.get("origin_node", ""),
                "submitted_at": mem.get("submitted_at", ""),
                "received_at":  received_at,
                "timestamp":    received_at,
            }
            core.qdrant.upsert(
                collection_name=core.COLLECTION,
                points=[PointStruct(id=canonical_id, vector=mem.get("vector"), payload=payload)],
            )
            inserted_ids.append(canonical_id)
        return inserted_ids

    # ── POST /push — share a local memory with every peer in group ───────
    def _handle_push(self):
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]
        membership = cfg["memberships"][0]
        group_name = membership["group_name"]
        peers      = membership["peers"]
        node_name  = cfg["node_name"]

        body, err = self._read_json()
        if err:
            return self._send_json(HTTP_BAD_REQUEST, {"error": err})
        if "record" not in body or "id" not in body["record"]:
            return self._send_json(HTTP_BAD_REQUEST, {"error": "missing record.id"})

        record   = body["record"]
        local_id = record["id"]

        # Augment local entry: re-upsert with group metadata added.
        existing = core.qdrant.retrieve(
            collection_name=core.COLLECTION,
            ids=[local_id], with_payload=True, with_vectors=True,
        )
        if not existing:
            return self._send_json(HTTP_NOT_FOUND, {"error": f"local memory {local_id} not found"})

        submitted_at = core.iso_now()
        p = existing[0]
        augmented = dict(p.payload or {})
        augmented["group_name"]   = group_name
        augmented["origin_node"]  = node_name
        augmented["submitted_at"] = submitted_at
        augmented["received_at"]  = submitted_at
        core.qdrant.upsert(
            collection_name=core.COLLECTION,
            points=[PointStruct(id=local_id, vector=p.vector, payload=augmented)],
        )
        log.info("PUSH augment: id=%s group=%s submitted_at=%s",
                 local_id[:8], group_name, submitted_at)

        wire = {
            "content":      record["content"],
            "content_hash": record["content_hash"],
            "vector":       record["vector"],
            "type":         record["type"],
            "tags":         record.get("tags", []),
            "project":      record.get("project", ""),
            "importance":   record["importance"],
            "group_name":   group_name,
            "origin_node":  node_name,
            "submitted_at": submitted_at,
        }

        results = []
        for peer in peers:
            r = {"node_name": peer["node_name"], "group_name": group_name}
            try:
                resp = httpx.post(f"{peer['endpoint']}/memory/put",
                                  json=wire, timeout=PEER_HTTP_TIMEOUT)
                if resp.status_code != HTTP_OK:
                    r["status"] = "unreachable"
                    r["reason"] = f"http {resp.status_code}"
                else:
                    r["status"]   = "responsive"
                    r["delivery"] = resp.json().get("status", "stored")
                    log.info("PUSH to %s: %s", peer["node_name"], r["delivery"])
            except httpx.ConnectError:
                r["status"], r["reason"] = "unreachable", "connection refused"
            except httpx.TimeoutException:
                r["status"], r["reason"] = "unreachable", "timeout"
            except Exception as e:
                r["status"], r["reason"] = "unreachable", str(e)[:200]
            results.append(r)

        results.sort(key=lambda r: r["node_name"])
        return self._send_json(HTTP_OK, {"peers": results})


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

    core.QDRANT_URL = cfg["qdrant_url"]
    from qdrant_client import QdrantClient
    core.qdrant = QdrantClient(url=cfg["qdrant_url"], timeout=10)

    log = core.configure_logging(DAEMON_NAME)
    log.info("=" * 60)
    log.info("Constellation daemon v%s starting", VERSION)
    log.info("  node_name:        %s", cfg["node_name"])
    log.info("  config:           %s", config_path)
    log.info("  state_dir:        %s", state_dir)
    log.info("  qdrant_url:       %s", cfg["qdrant_url"])
    log.info("  collection:       %s", core.COLLECTION)
    log.info("  peer_listener:    %s", cfg["peer_listen_address"])
    log.info("  gateway_listener: %s", cfg["gateway_listen_address"])
    for m in cfg["memberships"]:
        log.info("  membership:       %s (peers=%d)",
                 m["group_name"], len(m["peers"]))

    init_collection(core.COLLECTION, log)

    state = {"config": cfg, "log": log}
    PeerHandler.daemon_state = state
    GatewayHandler.daemon_state = state

    HTTPServer.allow_reuse_address = True

    peer_host, peer_port = cfg["peer_listen_address"].rsplit(":", 1)
    gw_host,   gw_port   = cfg["gateway_listen_address"].rsplit(":", 1)
    try:
        peer_server = HTTPServer((peer_host, int(peer_port)), PeerHandler)
    except OSError as e:
        print(f"ERROR: cannot bind peer listener {cfg['peer_listen_address']}: {e}",
              file=sys.stderr)
        sys.exit(1)
    try:
        gateway_server = HTTPServer((gw_host, int(gw_port)), GatewayHandler)
    except OSError as e:
        peer_server.server_close()
        print(f"ERROR: cannot bind gateway listener {cfg['gateway_listen_address']}: {e}",
              file=sys.stderr)
        sys.exit(1)

    peer_thread    = threading.Thread(target=peer_server.serve_forever,    daemon=True)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    peer_thread.start()
    gateway_thread.start()

    if args.foreground:
        print(f"✓ Constellation daemon v{VERSION}")
        print(f"  Node:        {cfg['node_name']}")
        print(f"  Peer:        http://{cfg['peer_listen_address']}    (peer-to-peer)")
        print(f"  Gateway:     http://{cfg['gateway_listen_address']}  (mem-fusion → constellation)")
        print(f"  Qdrant:      {cfg['qdrant_url']} ({core.COLLECTION})")
        print(f"  Membership:  {cfg['memberships'][0]['group_name']} "
              f"({len(cfg['memberships'][0]['peers'])} peers)")
        print(f"  Log:         {core.ACTIVE_LOG_PATH}")
        print(f"  Stop:        Ctrl-C")

    shutdown_event = threading.Event()
    def _shutdown(signum, _frame):
        log.info("Received signal %d, shutting down", signum)
        shutdown_event.set()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    shutdown_event.wait()
    peer_server.shutdown()
    gateway_server.shutdown()
    peer_thread.join(timeout=5)
    gateway_thread.join(timeout=5)
    peer_server.server_close()
    gateway_server.server_close()
    log.info("Constellation daemon stopped")


if __name__ == "__main__":
    main()
C088079119A2_EOF
chmod +x ~/.local/share/mem-fusion/constellation.py
```

---

## Step 3 — Write the config

The Constellation daemon needs a `config.json` describing this node's identity, listeners, group membership, and peer endpoints. Edit the values below, then write the file:

```bash
cat > ~/.local/share/mem-fusion/constellation/config.json <<'3079B827BF68_EOF'
{
  "node_name": "<your-machine-name>",

  "peer_listen_address":    "0.0.0.0:7533",
  "gateway_listen_address": "127.0.0.1:7534",

  "qdrant_url": "http://127.0.0.1:6333",

  "state_dir": "~/.local/share/mem-fusion/constellation",

  "memberships": [
    {
      "group_name": "<your-group-name>",
      "peers": [
        { "node_name": "<peer-2-name>", "endpoint": "http://<peer-2-ip>:7533" },
        { "node_name": "<peer-3-name>", "endpoint": "http://<peer-3-ip>:7533" }
      ]
    }
  ]
}
3079B827BF68_EOF
```

```bash
echo "Edit ~/.local/share/mem-fusion/constellation/config.json:"
echo "  - <your-machine-name>:  this peer's identifier"
echo "  - <your-group-name>:    the group this peer joins"
echo "  - peers[]:              other peers in the group (node_name + endpoint URL)"
echo ""
echo "The peer endpoints are HTTP URLs to each other peer's port-7533 listener."
echo "If you only have one peer right now, set peers to [] — you can add more later."
```

---

## Step 4 — Install the launchd plist

```bash
export HOME_LIT="$HOME"
```

```bash
cat > ~/Library/LaunchAgents/com.branchapp.memfusion.constellation.plist <<E5014F1E6228_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.branchapp.memfusion.constellation</string>
    <key>ProgramArguments</key>
    <array>
        <string>${HOME_LIT}/.local/share/mem-fusion/venv/bin/python</string>
        <string>${HOME_LIT}/.local/share/mem-fusion/constellation.py</string>
        <string>--config</string>
        <string>${HOME_LIT}/.local/share/mem-fusion/constellation/config.json</string>
        <string>--foreground</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>${HOME_LIT}/.local/share/mem-fusion/constellation/logs/constellation.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME_LIT}/.local/share/mem-fusion/constellation/logs/constellation-error.log</string>
    <key>WorkingDirectory</key>
    <string>${HOME_LIT}/.local/share/mem-fusion/constellation</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>${HOME_LIT}</string>
    </dict>
</dict>
</plist>
E5014F1E6228_EOF
```

---

## Step 5 — Start the Constellation daemon

```bash
launchctl load ~/Library/LaunchAgents/com.branchapp.memfusion.constellation.plist
curl -s --retry 12 --retry-delay 1 --retry-connrefused http://127.0.0.1:7533/health >/dev/null \
  && echo "  Peer listener up" \
  || { echo "Constellation failed to start — check ~/.local/share/mem-fusion/constellation/logs/"; exit 1; }
```

---

## Step 6 — Smoke test

```bash
curl -s http://127.0.0.1:7533/health     | python3 -m json.tool
curl -s http://127.0.0.1:7533/peers/self | python3 -m json.tool
curl -s -X POST http://127.0.0.1:7534/pull -d '{}' -H 'Content-Type: application/json' | python3 -m json.tool
```

The first two responses should show your `node_name`, `version`, and `memberships`. The third should return `{"peers": [...]}` — empty if no peers configured, or per-peer telemetry if peers are listed.

---

## Step 7 — Tell the user what to add to their CLAUDE.md

Print this snippet and instruct the user to paste it into `~/CLAUDE.md`:

```markdown
## Group Memory (Constellation)

Local Mem-Fusion is connected to a Constellation backend that shares memory
with other peers in your group. Two MCP tools on `mem-fusion`:

- `group_pull` — pull new memories from all peers in your group. Returns
  per-peer telemetry: `{peers: [{node_name, group_name, status, entry_ids?, reason?}]}`.
  Render a per-peer natural-language summary to the user; never dump the raw JSON.
  After pulling, use `export_record` or `search_recent` to surface specific
  entries when the user wants to see them.

- `group_push` — share a locally-stored memory with all peers in your group.
  Call this after `store_memory` returns an `id`, on memories the user wants
  to share. Returns per-peer delivery telemetry: `{peers: [{node_name,
  group_name, status, delivery?, reason?}]}`. Render a per-peer summary; never
  dump the raw JSON.

### When to push
On user "remember X" (the `/remember` skill flow), after `store_memory`
returns a local ID:
1. Call `mem-fusion/group_push(id=<local_id>)`.
2. Summarize: "Shared with N peers. Peer-X unreachable: <reason>" etc.

### When NOT to push
Personal preferences, machine-specific config, anything the user marked
local-only. Default for v0.3.0 is auto-push on explicit `/remember`.

### When to pull
On `/remember pull`, or when the user asks "what's new from the group?".
Render a per-peer summary of arrivals; surface specific entries the user
asks about with `export_record(id)`.
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Daemon won't start | Check `~/.local/share/mem-fusion/constellation/logs/`. Common causes: config placeholders not replaced; port 7533 or 7534 already in use. |
| `group_pull` / `group_push` returns `constellation_not_installed` | Daemon not running. `launchctl load ~/Library/LaunchAgents/com.branchapp.memfusion.constellation.plist`. |
| `/peers` returns empty | Peers are inferred from received-memory activity. Once any peer pushes to this node, they'll appear. |
| Port 7533 or 7534 conflict | Stop the other process, or pick different ports in `config.json` and update the launchd plist's `--config`. |
| Pull returns all peers as `unreachable` | Other peers' Constellation daemons aren't running, their endpoints in your config are wrong, or there's a network/firewall issue. |

Logs: `~/.local/share/mem-fusion/logs/constellation.<UTC-timestamp>.log`.

---

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.constellation.plist 2>/dev/null
rm ~/Library/LaunchAgents/com.branchapp.memfusion.constellation.plist

read -p "Delete the Constellation install directory? [y/N] " yn
[[ "$yn" == "y" ]] && rm -rf ~/.local/share/mem-fusion/constellation ~/.local/share/mem-fusion/constellation.py
```

Mem-Fusion is not affected — `group_pull` / `group_push` will simply return `constellation_not_installed` until Constellation is reinstalled.

---

## After install — final report to the user

When all 7 steps + smoke test pass, tell the user:

> Constellation is live on this peer. Paste the CLAUDE.md snippet from Step 7 into your `~/CLAUDE.md` so Claude knows when to push and pull group memory. Mem-Fusion now has two new MCP tools: `group_push` (called automatically after `/remember`) and `group_pull` (use `/remember pull` to invoke). To test group sharing, install Constellation on another machine pointing at the same `group_name` and add each peer's endpoint to the other's `peers` list, then say *"remember that we picked Qdrant for v0.3.0"* — the memory will land locally and be pushed to the other peer.
