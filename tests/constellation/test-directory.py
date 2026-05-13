#!/usr/bin/env python3
"""
Constellation directory test: orchestrator's directory endpoints (/peers and
/peers/self).

Drives the rewritten stack — peer-side reads via core.export_record (rebound
to each peer's Qdrant), federation via constellation.py HTTP. Confirms that
PUTs from multiple peers register correctly in the aggregated peer list.

Prerequisite: tests/constellation/preload-memories.py has been run.

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/constellation/test-directory.py
"""
import asyncio
import json
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from qdrant_client import QdrantClient

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
import core  # noqa: E402


ORCHESTRATOR_CONFIG = Path.home() / ".local/share/mem-fusion-dev/constellation/config.json"
DAEMON_URL          = "http://127.0.0.1:7533"
GROUP_NAME          = "wp2-test-group@dev"
NODE_NAME           = "mem-fusion-dev-test"
CANONICAL_QDRANT    = "http://127.0.0.1:6433"

PEERS_CFG = {
    "mem-fusion-peer-b": {"qdrant_url": "http://127.0.0.1:6533"},
    "mem-fusion-peer-c": {"qdrant_url": "http://127.0.0.1:6633"},
}

DAEMON_SCRIPT = REPO_ROOT / "src/constellation.py"
VENV_PYTHON   = Path.home() / ".local/share/cowork-memory/venv/bin/python"


def use_qdrant(url: str):
    core.QDRANT_URL = url
    core.qdrant = QdrantClient(url=url, timeout=10)


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
    """Drop the orchestrator's collection so each test starts clean."""
    try:
        httpx.delete(f"{CANONICAL_QDRANT}/collections/{core.COLLECTION}", timeout=5)
        print(f"  ✓ dropped orchestrator collection '{core.COLLECTION}'")
    except Exception as e:
        print(f"  (nothing to drop: {e})")


async def export_record(peer_name: str, memory_id: str) -> dict:
    """Use core.export_record after rebinding core to the peer's Qdrant."""
    use_qdrant(PEERS_CFG[peer_name]["qdrant_url"])
    r = await core.export_record({"id": memory_id})
    if "error" in r:
        raise RuntimeError(r["error"])
    return r


def list_local(peer_name: str) -> list[str]:
    use_qdrant(PEERS_CFG[peer_name]["qdrant_url"])
    pts, _ = core.qdrant.scroll(
        collection_name=core.COLLECTION, limit=20,
        with_payload=True, with_vectors=False,
    )
    return [str(p.id) for p in pts if (p.payload or {}).get("source") == "local"]


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


async def run_test():
    passed = []

    print("\nSTEP 2: GET /peers/self")
    r = httpx.get(f"{DAEMON_URL}/peers/self", timeout=5).json()
    print(f"  response: {json.dumps(r, indent=2)}")
    passed.append(check("self.node_name matches config", r["node_name"] == NODE_NAME))
    passed.append(check("self has version field", "version" in r))
    passed.append(check("self has listen_address", "listen_address" in r))
    passed.append(check("self lists our group as a flat name",
                        GROUP_NAME in r["memberships"]))

    print("\nSTEP 3: GET /peers with no federation entries — expect empty list")
    r = httpx.get(f"{DAEMON_URL}/peers",
                  params={"group_name": GROUP_NAME}, timeout=5).json()
    print(f"  response: {json.dumps(r, indent=2)}")
    passed.append(check("empty group: count == 0", r["count"] == 0))
    passed.append(check("empty group: peers == []", r["peers"] == []))
    passed.append(check("responding_node set", r["responding_node"] == NODE_NAME))

    print("\nSTEP 4: promote 1 memory from peer-b → /peers shows 1 member")
    peer_b_ids = list_local("mem-fusion-peer-b")
    if len(peer_b_ids) < 3:
        print(f"  ✗ peer-b needs >=3 local memories, has {len(peer_b_ids)}", file=sys.stderr)
        sys.exit(1)
    post_put("mem-fusion-peer-b", await export_record("mem-fusion-peer-b", peer_b_ids[0]))
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
    post_put("mem-fusion-peer-b", await export_record("mem-fusion-peer-b", peer_b_ids[1]))
    post_put("mem-fusion-peer-b", await export_record("mem-fusion-peer-b", peer_b_ids[2]))
    r = httpx.get(f"{DAEMON_URL}/peers",
                  params={"group_name": GROUP_NAME}, timeout=5).json()
    b_entry = next((p for p in r["peers"] if p["node_name"] == "mem-fusion-peer-b"), None)
    passed.append(check("peer-b submission_count == 3", b_entry and b_entry["submission_count"] == 3))
    if b_entry:
        passed.append(check("last_seen > first_seen after multi PUT",
                            b_entry["last_seen"] > b_entry["first_seen"],
                            f"first={b_entry['first_seen']!r} last={b_entry['last_seen']!r}"))

    print("\nSTEP 6: promote 1 from peer-c → peer count == 2")
    peer_c_ids = list_local("mem-fusion-peer-c")
    if len(peer_c_ids) < 1:
        print(f"  ✗ peer-c needs >=1 local memory", file=sys.stderr)
        sys.exit(1)
    post_put("mem-fusion-peer-c", await export_record("mem-fusion-peer-c", peer_c_ids[0]))
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
        print("\n✗ Directory verification FAILED", file=sys.stderr)
        sys.exit(1)
    print("\n" + "=" * 70)
    print("✓ Directory verification PASSED")
    print("=" * 70)


def main():
    print("=" * 70)
    print("Constellation directory — /peers and /peers/self via core+constellation")
    print("=" * 70)

    print("\nSTEP 1: clean slate (drop orchestrator collection, start daemon)")
    drop_canonical()
    daemon_proc = start_daemon()
    try:
        asyncio.run(run_test())
    finally:
        if daemon_proc is not None:
            print("\n[teardown] stopping daemon")
            daemon_proc.terminate()
            try:
                daemon_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                daemon_proc.kill()


if __name__ == "__main__":
    main()
