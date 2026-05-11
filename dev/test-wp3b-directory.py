#!/usr/bin/env python3
"""
WP3 Phase 3b verification: orchestrator's directory endpoints.

Validates GET /peers (members inferred from canonicals) and GET /peers/self
(this node's identity), and confirms that PUTs from multiple peers register
correctly in the aggregated peer list.

Prerequisite: dev/preload-test-memories.py has been run (provides peer-b
and peer-c with realistic memories that we then promote).

Flow:
  1. Drop canonical collection, start daemon (clean slate)
  2. GET /peers/self — assert this node's identity + group role
  3. GET /peers?group=X with no submissions — expect 0 peers
  4. Promote 1 memory from peer-b — GET /peers shows 1 member
  5. Promote 2 more memories from peer-b — submission_count == 3
  6. Promote 1 memory from peer-c — peer count == 2
  7. Negative: GET /peers for unknown group — 403
  8. Negative: GET /peers without group_name — 400

Usage:
  ~/.local/share/cowork-memory/venv/bin/python dev/test-wp3b-directory.py
"""
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from qdrant_client import QdrantClient

ORCHESTRATOR_CONFIG = Path.home() / ".local/share/mem-fusion-dev/constellation/config.json"
DAEMON_URL          = "http://127.0.0.1:7533"
GROUP_NAME          = "wp2-test-group@dev"
ORCHESTRATOR_NAME   = "mem-fusion-dev-test"
CANONICAL_QDRANT    = "http://127.0.0.1:6433"
CANONICAL_COLL      = "mem_fusion_canonical_dev_memories"

PEERS_CFG = {
    "mem-fusion-peer-b": {
        "qdrant_url": "http://127.0.0.1:6533",
        "collection": "mem_fusion_peer_b_memories",
    },
    "mem-fusion-peer-c": {
        "qdrant_url": "http://127.0.0.1:6633",
        "collection": "mem_fusion_peer_c_memories",
    },
}

DAEMON_SCRIPT = Path.home() / "dev/mem-fusion/extensions/constellation/constellation.py"
VENV_PYTHON   = Path.home() / ".local/share/cowork-memory/venv/bin/python"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def daemon_running() -> bool:
    try:
        return httpx.get(f"{DAEMON_URL}/health", timeout=1.0).status_code == 200
    except Exception:
        return False


def start_daemon():
    if daemon_running():
        print("  daemon already running, reusing")
        return None
    print(f"  starting daemon: {VENV_PYTHON} {DAEMON_SCRIPT}")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), str(DAEMON_SCRIPT),
         "--config", str(ORCHESTRATOR_CONFIG), "--foreground"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    for _ in range(15):
        if daemon_running():
            print(f"  ✓ daemon ready (pid {proc.pid})")
            return proc
        time.sleep(1)
    out, err = proc.communicate(timeout=2)
    print("STDOUT:", out.decode(), file=sys.stderr)
    print("STDERR:", err.decode(), file=sys.stderr)
    sys.exit(1)


def drop_canonical():
    """Drop the canonical collection so each test starts clean."""
    try:
        httpx.delete(f"{CANONICAL_QDRANT}/collections/{CANONICAL_COLL}", timeout=5)
        print(f"  ✓ dropped canonical collection")
    except Exception as e:
        print(f"  (no canonical to drop: {e})")


def export_record(peer_name: str, memory_id: str) -> dict:
    cfg = PEERS_CFG[peer_name]
    client = QdrantClient(url=cfg["qdrant_url"], timeout=10)
    pts = client.retrieve(collection_name=cfg["collection"], ids=[memory_id],
                          with_vectors=True, with_payload=True)
    if not pts:
        raise RuntimeError(f"{memory_id} not found in {peer_name}")
    p = pts[0]
    return {
        "id":           str(p.id),
        "vector":       list(p.vector),
        "content":      p.payload.get("content", ""),
        "content_hash": p.payload.get("content_hash", ""),
        "type":         p.payload.get("type", ""),
        "tags":         p.payload.get("tags", []),
        "importance":   p.payload.get("importance", 3),
        "timestamp":    p.payload.get("timestamp", ""),
    }


def list_preloaded(peer_name: str) -> list[str]:
    cfg = PEERS_CFG[peer_name]
    client = QdrantClient(url=cfg["qdrant_url"], timeout=10)
    pts, _ = client.scroll(collection_name=cfg["collection"],
                           limit=10, with_payload=True, with_vectors=False)
    return [str(p.id) for p in pts if p.payload.get("source") == "preload"]


def post_put(peer_name: str, record: dict) -> dict:
    body = {
        "group_name": GROUP_NAME,
        "memory_record": {
            "content":            record["content"],
            "vector":             record["vector"],
            "content_hash":       record["content_hash"],
            "type":               record["type"],
            "tags":               record["tags"],
            "importance":         record["importance"],
            "original_timestamp": record["timestamp"],
        },
        "provenance": {
            "origin_node":      peer_name,
            "origin_local_id":  record["id"],
            "submitted_at":     iso_now(),
            "submission_kind":  "wp3b-test",
        },
    }
    r = httpx.post(f"{DAEMON_URL}/memory/put", json=body, timeout=10.0)
    return {"status_code": r.status_code, "body": r.json()}


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "✓" if condition else "✗"
    extra = f" — {detail}" if (detail and not condition) else ""
    print(f"  {mark} {label}{extra}")
    return condition


def main():
    print("=" * 70)
    print("WP3 Phase 3b — directory endpoints (/peers and /peers/self)")
    print("=" * 70)

    print("\nSTEP 1: clean slate (drop canonical, start daemon)")
    drop_canonical()
    daemon_proc = start_daemon()
    try:
        run_test()
    finally:
        if daemon_proc is not None:
            print("\n[teardown] stopping daemon")
            daemon_proc.terminate()
            try:
                daemon_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                daemon_proc.kill()


def run_test():
    passed = []

    print("\nSTEP 2: GET /peers/self")
    r = httpx.get(f"{DAEMON_URL}/peers/self", timeout=5).json()
    print(f"  response: {json.dumps(r, indent=2)}")
    passed.append(check("self.node_name matches config", r["node_name"] == ORCHESTRATOR_NAME))
    passed.append(check("self has version field", "version" in r))
    passed.append(check("self has listen_address", "listen_address" in r))
    passed.append(check("self lists our group with orchestrator role",
                        any(m["group_name"] == GROUP_NAME and m["role"] == "orchestrator"
                            for m in r["memberships"])))

    print("\nSTEP 3: GET /peers with no canonicals — expect empty list")
    r = httpx.get(f"{DAEMON_URL}/peers",
                  params={"group_name": GROUP_NAME}, timeout=5).json()
    print(f"  response: {json.dumps(r, indent=2)}")
    passed.append(check("empty group: count == 0", r["count"] == 0))
    passed.append(check("empty group: peers == []", r["peers"] == []))
    passed.append(check("orchestrator field set", r["orchestrator"] == ORCHESTRATOR_NAME))

    print("\nSTEP 4: promote 1 memory from peer-b → /peers shows 1 member")
    peer_b_ids = list_preloaded("mem-fusion-peer-b")
    if len(peer_b_ids) < 3:
        print(f"  ✗ peer-b needs >=3 preloaded memories, has {len(peer_b_ids)}", file=sys.stderr)
        sys.exit(1)
    post_put("mem-fusion-peer-b", export_record("mem-fusion-peer-b", peer_b_ids[0]))
    r = httpx.get(f"{DAEMON_URL}/peers",
                  params={"group_name": GROUP_NAME}, timeout=5).json()
    passed.append(check("after 1 PUT: count == 1", r["count"] == 1))
    if r["count"] >= 1:
        b = r["peers"][0]
        passed.append(check("peer-b appears", b["node_name"] == "mem-fusion-peer-b"))
        passed.append(check("submission_count == 1", b["submission_count"] == 1))
        passed.append(check("first_seen and last_seen match (single PUT)",
                            b["first_seen"] == b["last_seen"]))

    print("\nSTEP 5: promote 2 more from peer-b → submission_count == 3")
    post_put("mem-fusion-peer-b", export_record("mem-fusion-peer-b", peer_b_ids[1]))
    post_put("mem-fusion-peer-b", export_record("mem-fusion-peer-b", peer_b_ids[2]))
    r = httpx.get(f"{DAEMON_URL}/peers",
                  params={"group_name": GROUP_NAME}, timeout=5).json()
    b_entry = next((p for p in r["peers"] if p["node_name"] == "mem-fusion-peer-b"), None)
    passed.append(check("peer-b submission_count == 3", b_entry and b_entry["submission_count"] == 3))
    if b_entry:
        passed.append(check("last_seen > first_seen after multi PUT",
                            b_entry["last_seen"] > b_entry["first_seen"],
                            f"first={b_entry['first_seen']!r} last={b_entry['last_seen']!r}"))

    print("\nSTEP 6: promote 1 from peer-c → peer count == 2")
    peer_c_ids = list_preloaded("mem-fusion-peer-c")
    if len(peer_c_ids) < 1:
        print(f"  ✗ peer-c needs >=1 preloaded memory", file=sys.stderr)
        sys.exit(1)
    post_put("mem-fusion-peer-c", export_record("mem-fusion-peer-c", peer_c_ids[0]))
    r = httpx.get(f"{DAEMON_URL}/peers",
                  params={"group_name": GROUP_NAME}, timeout=5).json()
    print(f"  response: {json.dumps(r, indent=2)}")
    passed.append(check("after peer-c PUT: count == 2", r["count"] == 2))
    passed.append(check("peer-c appears in list",
                        any(p["node_name"] == "mem-fusion-peer-c" for p in r["peers"])))
    passed.append(check("peer-b still shows submission_count == 3",
                        any(p["node_name"] == "mem-fusion-peer-b" and p["submission_count"] == 3
                            for p in r["peers"])))
    passed.append(check("peers sorted by last_seen desc",
                        r["peers"][0]["last_seen"] >= r["peers"][1]["last_seen"]))

    print("\nSTEP 7: negative — /peers for unknown group → 403")
    r = httpx.get(f"{DAEMON_URL}/peers",
                  params={"group_name": "bogus@group"}, timeout=5)
    passed.append(check("unknown group returns 403", r.status_code == 403))

    print("\nSTEP 8: negative — /peers without group_name → 400")
    r = httpx.get(f"{DAEMON_URL}/peers", timeout=5)
    passed.append(check("missing group_name returns 400", r.status_code == 400))

    total = len(passed)
    ok = sum(passed)
    print(f"\n  {ok}/{total} invariants passed")
    if ok != total:
        print("\n✗ Phase 3b verification FAILED", file=sys.stderr)
        sys.exit(1)
    print("\n" + "=" * 70)
    print("✓ Phase 3b verification PASSED")
    print("=" * 70)


if __name__ == "__main__":
    main()
