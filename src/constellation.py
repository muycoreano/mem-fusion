#!/usr/bin/env python3
"""
Constellation Daemon — group memory backend for a single Mem-Fusion node.

Two HTTP listeners in one process:

  PEER surface — bound to peer_listen_address (default 0.0.0.0:7533).
    What other peers in groups containing this node call to deliver memory.
      GET  /health                    — liveness
      GET  /peers/self                — this node's identity + memberships
      GET  /peers?group_name=         — directory inferred from received entries
      POST /memory/put                — receive a memory from another peer
      GET  /memory/get?group_name=    — fetch entries in a group by id or scroll
      GET  /memory/since?group_name=&cursor=  — pull catch-up since cursor

  GATEWAY surface — bound to gateway_listen_address (default 127.0.0.1:7534).
    What the local mem-fusion process calls to drive group operations.
    Localhost-only by network binding.
      POST /pull   — pull new memories. Body {group?: str}.
                     Omit group to iterate every configured membership.
      POST /push   — share local memories with peers in one group.
                     Body {group: str, memory_ids?: list[str]}.
                     memory_ids filters which memories to push; omit to push
                     every local entry tagged with that group.

Storage model (shared Qdrant collection `cowork_memories`):
  Every entry carries `groups: list[str]` (the routing key for sharing) plus
  `origin_node` and `submitted_at` set at store time. Memories are never
  augmented in place at push time — they're born complete on the local node.

Dedup is on `content_hash` (global, group-agnostic). On a hash hit during
/memory/put, the existing local entry's `groups` is additively widened with
the incoming `groups` — never shrunk. Same rule applied during /pull merge.

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


VERSION          = "0.5.0-alpha"
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
    """Wire shape for an entry returned by peer-side /memory/get."""
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
        "groups":       core.entry_groups(pl),
        "origin_node":  pl.get("origin_node", ""),
        "submitted_at": pl.get("submitted_at", ""),
        "received_at":  pl.get("received_at", ""),
    }


# ── Config loading ─────────────────────────────────────────────────────────
def load_config(path):
    """Load and validate the daemon's config.

    Required: node_name, memberships[].group_name, memberships[].peers[].
    Each peer entry: {node_name, endpoint}. v0.4 supports multi-membership
    — a node can belong to any number of groups simultaneously, including
    the implicit `personal` group with its own (optional) peer list for
    cross-machine personal sync.
    """
    with open(path) as f:
        cfg = json.load(f)

    if "node_name" not in cfg:
        raise ValueError("config missing required field: node_name")
    if "memberships" not in cfg or not isinstance(cfg["memberships"], list):
        raise ValueError("config missing or invalid 'memberships' (must be a list)")

    seen_groups = set()
    for i, m in enumerate(cfg["memberships"]):
        if "group_name" not in m:
            raise ValueError(f"memberships[{i}] missing field: group_name")
        if m["group_name"] in seen_groups:
            raise ValueError(f"duplicate membership for group {m['group_name']!r}")
        seen_groups.add(m["group_name"])
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


# ── Membership lookups ────────────────────────────────────────────────────
def all_groups(cfg) -> list[str]:
    return [m["group_name"] for m in cfg["memberships"]]


def find_membership(cfg, group_name):
    """Return the membership entry for a group_name, or None."""
    for m in cfg["memberships"]:
        if m["group_name"] == group_name:
            return m
    return None


def peer_groups(cfg, peer_node_name) -> set:
    """Return the set of groups peer_node_name appears in, from this node's view.

    Used by push-time group filter: when sending memory M to peer P, the wire
    `groups` field is intersected with peer_groups(cfg, P) so we never expose
    a group tag P isn't a member of (notably this node's `personal`).
    """
    return {m["group_name"] for m in cfg["memberships"]
            if any(p["node_name"] == peer_node_name for p in m["peers"])}


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
        "groups":       PayloadSchemaType.KEYWORD,  # v0.4: list-valued routing key
        "group_name":   PayloadSchemaType.KEYWORD,  # legacy v0.3, kept for migration
        "source":       PayloadSchemaType.KEYWORD,  # legacy v0.3
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
            "memberships": all_groups(cfg),
        })

    # ── /memory/put — receive a memory from another peer ─────────────────
    def _handle_memory_put(self):
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]

        body, err = self._read_json()
        if err:
            return self._send_json(HTTP_BAD_REQUEST, {"error": err})

        for k in ("content", "content_hash", "vector", "type", "importance",
                  "groups", "origin_node", "submitted_at"):
            if k not in body:
                return self._send_json(HTTP_BAD_REQUEST, {"error": f"missing field: {k}"})

        incoming_groups = body["groups"]
        if not isinstance(incoming_groups, list) or not incoming_groups:
            return self._send_json(HTTP_BAD_REQUEST,
                                   {"error": "groups must be a non-empty list[str]"})

        my_groups = set(all_groups(cfg))
        # Sender pre-filtered `groups` to ones this node is a member of.
        # Anything outside that set is a protocol violation worth rejecting
        # so misconfiguration shows up loudly rather than silently storing.
        unknown = [g for g in incoming_groups if g not in my_groups]
        if unknown:
            return self._send_json(HTTP_FORBIDDEN, {
                "error":          "groups contain memberships this node doesn't have",
                "unknown_groups": unknown,
                "memberships":    sorted(my_groups),
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

        # Dedup globally on content_hash. Additive merge: if we already have
        # the content, widen its groups list with anything new from incoming.
        existing = core.find_existing_by_hash(body["content_hash"])
        if existing:
            existing_id, existing_payload = existing
            current = core.entry_groups(existing_payload)
            merged  = core.union_groups(current, incoming_groups)
            if merged == current:
                log.info("PUT duplicate: hash=%s existing=%s groups=%s",
                         body["content_hash"], existing_id, current)
                return self._send_json(HTTP_OK, {
                    "status": "duplicate", "id": existing_id, "groups": current,
                })
            core.qdrant.set_payload(
                collection_name=core.COLLECTION,
                payload={"groups": merged}, points=[existing_id],
            )
            log.info("PUT merged: hash=%s id=%s groups=%s",
                     body["content_hash"], existing_id, merged)
            return self._send_json(HTTP_OK, {
                "status": "merged", "id": existing_id, "groups": merged,
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
            "groups":       core.union_groups(incoming_groups),
            "origin_node":  body["origin_node"],
            "submitted_at": body["submitted_at"],
            "received_at":  received_at,
            # `timestamp` mirrors `received_at` so mem-fusion's search_recent
            # surfaces freshly-received entries without code knowing they're remote.
            "timestamp":    received_at,
        }
        core.qdrant.upsert(
            collection_name=core.COLLECTION,
            points=[PointStruct(id=new_id, vector=vec, payload=payload)],
        )
        log.info("PUT stored: id=%s groups=%s origin=%s",
                 new_id, payload["groups"], body["origin_node"])
        return self._send_json(HTTP_OK, {
            "status": "stored", "id": new_id, "groups": payload["groups"],
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

        memberships = all_groups(cfg)
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
            if not points:
                return self._send_json(HTTP_NOT_FOUND, {
                    "error": f"memory {memory_id} not found",
                })
            if group_name not in core.entry_groups(points[0].payload or {}):
                return self._send_json(HTTP_NOT_FOUND, {
                    "error": f"memory {memory_id} not in group {group_name!r}",
                })
            log.info("GET by-id: group=%s id=%s", group_name, memory_id)
            return self._send_json(HTTP_OK, {"count": 1, "memories": [point_to_record(points[0])]})

        points, _ = core.qdrant.scroll(
            collection_name=core.COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="groups", match=MatchValue(value=group_name)),
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

        memberships = all_groups(cfg)
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

        memberships = all_groups(cfg)
        if group_name not in memberships:
            return self._send_json(HTTP_FORBIDDEN, {
                "error":       f"this node is not a member of {group_name!r}",
                "memberships": memberships,
            })

        seen = {}
        for p in scroll_all(
            core.COLLECTION,
            Filter(must=[FieldCondition(key="groups", match=MatchValue(value=group_name))]),
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
            "memberships":    all_groups(cfg),
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

    # ── POST /pull — multi-group catch-up from peers ─────────────────────
    def _handle_pull(self):
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]

        body, _ = self._read_json()
        body = body or {}
        target_group = body.get("group")

        if target_group:
            membership = find_membership(cfg, target_group)
            if membership is None:
                return self._send_json(HTTP_FORBIDDEN, {
                    "error":       f"this node is not a member of {target_group!r}",
                    "memberships": all_groups(cfg),
                })
            memberships_to_pull = [membership]
        else:
            memberships_to_pull = [m for m in cfg["memberships"] if m["peers"]]

        results = []
        for membership in memberships_to_pull:
            group_name = membership["group_name"]
            peers      = membership["peers"]
            # NOTE (fix 0.5.0-013): cursor model dropped. The previous
            # max(submitted_at)-in-group cursor was correctness-broken — it
            # filtered out back-filled entries whose submitted_at fell below
            # our local max (e.g., peer Y's older memory propagating through
            # relay X after our last pull). Each pull now full-scans the
            # peer's group; receiver-side content_hash dedup (in
            # _merge_pulled below) handles efficiency. Revisit if any group
            # exceeds ~50K entries.
            log.info("PULL start: group=%s peers=%d (full-scan, no cursor)",
                     group_name, len(peers))

            for peer in peers:
                r = {"node_name": peer["node_name"], "group_name": group_name}
                try:
                    params = {"group_name": group_name}
                    resp = httpx.get(f"{peer['endpoint']}/memory/since",
                                     params=params, timeout=PEER_HTTP_TIMEOUT)
                    if resp.status_code != HTTP_OK:
                        r["status"] = "unreachable"
                        r["reason"] = f"http {resp.status_code}"
                    else:
                        merged_results = self._merge_pulled(
                            resp.json().get("memories", []), group_name)
                        r["status"] = "responsive"
                        r["entry_ids"] = [m["id"] for m in merged_results if m["status"] == "stored"]
                        r["merged_ids"] = [m["id"] for m in merged_results if m["status"] == "merged"]
                        log.info("PULL from %s (group=%s): stored=%d merged=%d",
                                 peer["node_name"], group_name,
                                 len(r["entry_ids"]), len(r["merged_ids"]))
                except httpx.ConnectError:
                    r["status"], r["reason"] = "unreachable", "connection refused"
                except httpx.TimeoutException:
                    r["status"], r["reason"] = "unreachable", "timeout"
                except Exception as e:
                    r["status"], r["reason"] = "unreachable", str(e)[:200]
                results.append(r)

        results.sort(key=lambda r: (r["group_name"], r["node_name"]))
        return self._send_json(HTTP_OK, {"peers": results})

    def _merge_pulled(self, memories, group_name):
        """Insert/merge pulled entries via global content_hash dedup + additive
        group merge. Returns per-memory results [{id, status}] where status is
        one of stored | merged | duplicate."""
        out = []
        for mem in memories:
            chash = mem.get("content_hash", "")
            incoming_groups = mem.get("groups") or [group_name]
            existing = core.find_existing_by_hash(chash)
            if existing:
                existing_id, existing_payload = existing
                current = core.entry_groups(existing_payload)
                merged  = core.union_groups(current, incoming_groups)
                if merged == current:
                    out.append({"id": existing_id, "status": "duplicate"})
                else:
                    core.qdrant.set_payload(
                        collection_name=core.COLLECTION,
                        payload={"groups": merged}, points=[existing_id])
                    out.append({"id": existing_id, "status": "merged"})
                continue

            canonical_id = str(uuid.uuid4())
            received_at  = core.iso_now()
            payload = {
                "content":      mem.get("content", ""),
                "content_hash": chash,
                "type":         mem.get("type", ""),
                "tags":         mem.get("tags", []),
                "project":      mem.get("project", ""),
                "importance":   mem.get("importance", 3),
                "groups":       core.union_groups(incoming_groups),
                "origin_node":  mem.get("origin_node", ""),
                "submitted_at": mem.get("submitted_at", ""),
                "received_at":  received_at,
                "timestamp":    received_at,
            }
            core.qdrant.upsert(
                collection_name=core.COLLECTION,
                points=[PointStruct(id=canonical_id, vector=mem.get("vector"),
                                    payload=payload)],
            )
            out.append({"id": canonical_id, "status": "stored"})
        return out

    # ── POST /push — share local memories with peers in one group ────────
    def _handle_push(self):
        cfg = self.daemon_state["config"]
        log = self.daemon_state["log"]
        node_name = cfg["node_name"]

        body, err = self._read_json()
        if err:
            return self._send_json(HTTP_BAD_REQUEST, {"error": err})

        target_group = (body or {}).get("group")
        memory_ids   = (body or {}).get("memory_ids")
        if not target_group:
            return self._send_json(HTTP_BAD_REQUEST,
                                   {"error": "missing field: group"})

        membership = find_membership(cfg, target_group)
        if membership is None:
            return self._send_json(HTTP_FORBIDDEN, {
                "error":       f"this node is not a member of {target_group!r}",
                "memberships": all_groups(cfg),
            })
        peers = membership["peers"]

        # Resolve candidate memories: by id list (filtered to those tagged with
        # target_group) or scroll all entries where target_group ∈ groups.
        if memory_ids:
            if not isinstance(memory_ids, list):
                return self._send_json(HTTP_BAD_REQUEST,
                                       {"error": "memory_ids must be a list[str]"})
            points = core.qdrant.retrieve(
                collection_name=core.COLLECTION, ids=memory_ids,
                with_payload=True, with_vectors=True,
            )
            candidates = [p for p in points
                          if target_group in core.entry_groups(p.payload or {})]
            skipped_ids = [mid for mid in memory_ids
                           if mid not in {str(p.id) for p in candidates}]
        else:
            candidates = []
            offset = None
            while True:
                pts, offset = core.qdrant.scroll(
                    collection_name=core.COLLECTION,
                    scroll_filter=Filter(must=[
                        FieldCondition(key="groups",
                                       match=MatchValue(value=target_group)),
                    ]),
                    limit=SCROLL_PAGE_SIZE, offset=offset,
                    with_payload=True, with_vectors=True,
                )
                candidates.extend(pts)
                if offset is None:
                    break
            skipped_ids = []

        log.info("PUSH start: group=%s candidates=%d peers=%d skipped=%d",
                 target_group, len(candidates), len(peers), len(skipped_ids))

        results = []
        for peer in peers:
            peer_seen_groups = peer_groups(cfg, peer["node_name"])
            r = {"node_name": peer["node_name"], "group_name": target_group}
            deliveries = []
            try:
                for p in candidates:
                    pl = p.payload or {}
                    memory_groups = core.entry_groups(pl)
                    wire_groups   = [g for g in memory_groups
                                     if g in peer_seen_groups]
                    if not wire_groups:
                        deliveries.append({
                            "memory_id": str(p.id),
                            "status":    "skipped",
                            "reason":    "no overlapping group with peer",
                        })
                        continue
                    wire = {
                        "content":      pl.get("content", ""),
                        "content_hash": pl.get("content_hash", ""),
                        "vector":       list(p.vector) if p.vector is not None else None,
                        "type":         pl.get("type", ""),
                        "tags":         pl.get("tags", []),
                        "project":      pl.get("project", ""),
                        "importance":   pl.get("importance", 3),
                        "groups":       wire_groups,
                        "origin_node":  pl.get("origin_node", node_name),
                        "submitted_at": pl.get("submitted_at", pl.get("timestamp", "")),
                    }
                    resp = httpx.post(f"{peer['endpoint']}/memory/put",
                                      json=wire, timeout=PEER_HTTP_TIMEOUT)
                    if resp.status_code != HTTP_OK:
                        deliveries.append({
                            "memory_id": str(p.id),
                            "status":    "error",
                            "reason":    f"http {resp.status_code}",
                        })
                    else:
                        rj = resp.json()
                        deliveries.append({
                            "memory_id": str(p.id),
                            "status":    rj.get("status", "stored"),
                            "remote_id": rj.get("id"),
                        })
                r["status"]     = "responsive"
                r["deliveries"] = deliveries
                log.info("PUSH to %s: %d deliveries", peer["node_name"], len(deliveries))
            except httpx.ConnectError:
                r["status"], r["reason"] = "unreachable", "connection refused"
            except httpx.TimeoutException:
                r["status"], r["reason"] = "unreachable", "timeout"
            except Exception as e:
                r["status"], r["reason"] = "unreachable", str(e)[:200]
            results.append(r)

        results.sort(key=lambda r: r["node_name"])
        return self._send_json(HTTP_OK, {
            "group":   target_group,
            "peers":   results,
            "skipped": skipped_ids,
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
    core.migrate_legacy_entries(log)

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
        for m in cfg["memberships"]:
            print(f"  Membership:  {m['group_name']} ({len(m['peers'])} peers)")
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
