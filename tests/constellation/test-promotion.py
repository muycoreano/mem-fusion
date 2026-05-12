#!/usr/bin/env python3
"""
Constellation promotion test: peer promotes a memory via constellation;
orchestrator stores it; peer reads it back and verifies byte-identity
(content + vector + content_hash).

Drives the rewritten stack:
  - Peer-side data access:        core.export_record (via core.qdrant rebound
                                  to peer-b's port)
  - Federation transport:         constellation.py HTTP /memory/put + /memory/get
  - Orchestrator-side dedup:      constellation.py against its Qdrant on :6433

mem_fusion.py (the stdio MCP for Claude) is *not* exercised here — peer-to-peer
tests don't involve Claude. See tests/mem_fusion/ for MCP-level tests.

Prerequisite: tests/constellation/preload-memories.py has been run.

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/constellation/test-promotion.py
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


# ── Config ─────────────────────────────────────────────────────────────────
ORCHESTRATOR_CONFIG = Path.home() / ".local/share/mem-fusion-dev/constellation/config.json"
DAEMON_URL          = "http://127.0.0.1:7533"
GROUP_NAME          = "wp2-test-group@dev"

PEER_B_QDRANT_URL   = "http://127.0.0.1:6533"
PEER_B_NAME         = "mem-fusion-peer-b"

DAEMON_SCRIPT       = REPO_ROOT / "src/constellation.py"
VENV_PYTHON         = Path.home() / ".local/share/cowork-memory/venv/bin/python"


def use_qdrant(url: str):
    """Rebind core's module-level Qdrant client (same pattern as constellation.py)."""
    core.QDRANT_URL = url
    core.qdrant = QdrantClient(url=url, timeout=10)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def daemon_running() -> bool:
    try:
        return httpx.get(f"{DAEMON_URL}/health", timeout=1.0).status_code == 200
    except Exception:
        return False


def start_daemon() -> subprocess.Popen | None:
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
    print("  ✗ daemon failed to become ready", file=sys.stderr)
    out, err = proc.communicate(timeout=2)
    print("STDOUT:", out.decode(), file=sys.stderr)
    print("STDERR:", err.decode(), file=sys.stderr)
    sys.exit(1)


async def pick_source_memory() -> dict:
    """Pick a locally-stored memory from peer-b via core.qdrant (rebound)."""
    use_qdrant(PEER_B_QDRANT_URL)
    points, _ = core.qdrant.scroll(
        collection_name=core.COLLECTION,
        limit=10, with_payload=True, with_vectors=False,
    )
    locals_only = [p for p in points if (p.payload or {}).get("source") == "local"]
    if not locals_only:
        print(f"  ✗ no local memories in {core.COLLECTION} on peer-b — run preload first", file=sys.stderr)
        sys.exit(1)
    chosen = locals_only[0]
    print(f"  ✓ picked memory {str(chosen.id)[:8]}…")
    print(f"    content: {chosen.payload['content'][:70]}…")
    return {"id": str(chosen.id), "payload": chosen.payload}


async def export_full_record(memory_id: str) -> dict:
    """Use core.export_record — the same path mem_fusion.py exposes via MCP."""
    r = await core.export_record({"id": memory_id})
    if "error" in r:
        raise RuntimeError(r["error"])
    return r


def post_to_daemon(local_record: dict) -> dict:
    body = {
        "group_name": GROUP_NAME,
        "memory_record": {
            "content":            local_record["content"],
            "vector":             local_record["vector"],
            "content_hash":       local_record["content_hash"],
            "type":               local_record["type"],
            "tags":               local_record["tags"],
            "importance":         local_record["importance"],
            "original_timestamp": local_record["timestamp"],
        },
        "provenance": {
            "origin_node":      PEER_B_NAME,
            "origin_local_id":  local_record["id"],
            "submitted_at":     iso_now(),
            "submission_kind":  "default-share",
        },
    }
    r = httpx.post(f"{DAEMON_URL}/memory/put", json=body, timeout=10.0)
    return {"status_code": r.status_code, "body": r.json()}


def get_from_daemon(canonical_id: str) -> dict:
    r = httpx.get(
        f"{DAEMON_URL}/memory/get",
        params={"group_name": GROUP_NAME, "id": canonical_id},
        timeout=10.0,
    )
    return {"status_code": r.status_code, "body": r.json()}


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "✓" if condition else "✗"
    extra = f" — {detail}" if (detail and not condition) else ""
    print(f"  {mark} {label}{extra}")
    return condition


async def run_test():
    print("\nSTEP 2: pick a local memory from peer-b (via core.qdrant)")
    src = await pick_source_memory()

    print("\nSTEP 3: export full record (via core.export_record)")
    record = await export_full_record(src["id"])
    print(f"  ✓ vector dim: {len(record['vector'])}")
    print(f"  ✓ content_hash: {record['content_hash']}")

    print("\nSTEP 4: POST /memory/put (promote to orchestrator)")
    put_result = post_to_daemon(record)
    print(f"  HTTP {put_result['status_code']}: {json.dumps(put_result['body'], indent=2)}")
    if put_result["status_code"] != 200:
        print("  ✗ PUT failed", file=sys.stderr)
        sys.exit(1)
    canonical_id = put_result["body"]["canonical_id"]
    put_status = put_result["body"]["status"]

    print(f"\nSTEP 5: GET /memory/get?id={canonical_id[:8]}…")
    get_result = get_from_daemon(canonical_id)
    print(f"  HTTP {get_result['status_code']}")
    if get_result["status_code"] != 200 or get_result["body"]["count"] != 1:
        print(f"  ✗ GET failed: {get_result['body']}", file=sys.stderr)
        sys.exit(1)
    canonical = get_result["body"]["memories"][0]

    print("\nSTEP 6: verify invariants")
    passed = sum([
        check("PUT returned 'stored' on fresh content", put_status == "stored",
              f"got status={put_status!r}"),
        check("canonical content matches source byte-identically",
              canonical["content"] == record["content"]),
        check("canonical content_hash matches source",
              canonical["content_hash"] == record["content_hash"]),
        check("canonical content_hash matches recomputed (integrity)",
              canonical["content_hash"] == core.content_hash(canonical["content"])),
        check("canonical vector length is 768",
              isinstance(canonical["vector"], list) and len(canonical["vector"]) == 768),
        check("canonical vector matches source byte-identically",
              canonical["vector"] == record["vector"]),
        check("canonical type preserved", canonical["type"] == record["type"]),
        check("canonical tags preserved", canonical["tags"] == record["tags"]),
        check("canonical importance preserved",
              canonical["importance"] == record["importance"]),
        check("canonical original_timestamp preserved",
              canonical["original_timestamp"] == record["timestamp"]),
        check("provenance.origin_node preserved",
              canonical["origin_node"] == PEER_B_NAME),
        check("provenance.origin_local_id preserved",
              canonical["origin_local_id"] == record["id"]),
        check("canonical has its own canonical_id",
              canonical["canonical_id"] == canonical_id and canonical_id != record["id"]),
        check("group_name attached", canonical["group_name"] == GROUP_NAME),
    ])

    print(f"\n  {passed}/14 invariants passed")
    if passed != 14:
        print("\n✗ Promotion verification FAILED", file=sys.stderr)
        sys.exit(1)

    print("\nSTEP 7: re-PUT same content → expect 'duplicate'")
    put2 = post_to_daemon(record)
    if put2["body"]["status"] == "duplicate" and put2["body"]["canonical_id"] == canonical_id:
        print(f"  ✓ duplicate detected; existing canonical_id returned")
    else:
        print(f"  ✗ expected duplicate, got: {put2['body']}", file=sys.stderr)
        sys.exit(1)

    print("\nSTEP 8: negative — content_hash mismatch should be rejected")
    tampered_record = {**record, "content_hash": "deadbeef" * 2}
    body = {
        "group_name": GROUP_NAME,
        "memory_record": {
            "content":            tampered_record["content"],
            "vector":             tampered_record["vector"],
            "content_hash":       tampered_record["content_hash"],
            "type":               tampered_record["type"],
            "tags":               tampered_record["tags"],
            "importance":         tampered_record["importance"],
            "original_timestamp": tampered_record["timestamp"],
        },
        "provenance": {
            "origin_node":      PEER_B_NAME,
            "origin_local_id":  record["id"],
            "submitted_at":     iso_now(),
            "submission_kind":  "tamper-test",
        },
    }
    r = httpx.post(f"{DAEMON_URL}/memory/put", json=body, timeout=10.0)
    if r.status_code == 400 and "content_hash mismatch" in r.json().get("error", ""):
        print(f"  ✓ tampered content_hash rejected with 400")
    else:
        print(f"  ✗ expected 400 mismatch, got: HTTP {r.status_code} {r.json()}", file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 70)
    print("✓ Promotion verification PASSED")
    print("=" * 70)


def main():
    print("=" * 70)
    print("Constellation promotion — peer promotes via core+constellation, reads it back")
    print("=" * 70)

    print("\nSTEP 1: clean orchestrator slate + ensure daemon is running")
    try:
        httpx.delete(f"http://127.0.0.1:6433/collections/{core.COLLECTION}", timeout=5)
        print(f"  ✓ dropped orchestrator collection '{core.COLLECTION}'")
    except Exception as e:
        print(f"  (nothing to drop: {e})")
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
