#!/usr/bin/env python3
"""
Constellation promotion test: a peer pushes a memory; the receiver stores it
and serves it back via /memory/get with byte-identical content + vector.
Verifies integrity invariants and content_hash mismatch rejection.

Self-contained — spins up one Qdrant + one Constellation daemon via the
shared harness. Simulates the originator/receiver split by inserting a
local memory via core.store_memory, exporting it via core.export_record,
then POSTing to /memory/put with a different origin_node (as if from
another peer).

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/constellation/test-promotion.py
"""
import asyncio
import json
import pathlib
import sys
import tempfile
from datetime import datetime, timezone

import httpx
from qdrant_client import QdrantClient

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "lib"))
import core      # noqa: E402
import harness   # noqa: E402


GROUP_NAME      = "test-promotion@e2e"
RECEIVER        = {"node_name": "receiver-node", "qdrant_port": 6733,
                   "peer_port": 7733, "gateway_port": 7734}
SIMULATED_PEER  = "originator-peer"


def use_qdrant(port):
    url = f"http://127.0.0.1:{port}"
    core.QDRANT_URL = url
    core.qdrant = QdrantClient(url=url, timeout=10)


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def post_put(record, *, origin_node, group_name=GROUP_NAME,
             content_hash_override=None):
    """POST a /memory/put body shaped like another peer sent it."""
    body = {
        "content":      record["content"],
        "content_hash": content_hash_override or record["content_hash"],
        "vector":       record["vector"],
        "type":         record["type"],
        "tags":         record["tags"],
        "project":      record.get("project", ""),
        "importance":   record["importance"],
        "group_name":   group_name,
        "origin_node":  origin_node,
        "submitted_at": iso_now(),
    }
    r = httpx.post(f"http://127.0.0.1:{RECEIVER['peer_port']}/memory/put",
                   json=body, timeout=10.0)
    return {"status_code": r.status_code, "body": r.json()}


def get_by_id(memory_id):
    r = httpx.get(f"http://127.0.0.1:{RECEIVER['peer_port']}/memory/get",
                  params={"group_name": GROUP_NAME, "id": memory_id},
                  timeout=10.0)
    return {"status_code": r.status_code, "body": r.json()}


async def run_tests(passed):
    print("\nSTEP 3: insert a memory locally (originator's side)")
    use_qdrant(RECEIVER["qdrant_port"])
    r = await core.store_memory({
        "content":    "The orchestrator role is a per-group assignment, not a per-node type.",
        "type":       "context",
        "tags":       ["constellation", "architecture"],
        "project":    "mem-fusion",
        "importance": 4,
    })
    if r.get("status") != "stored":
        print(f"  ✗ store_memory failed: {r}", file=sys.stderr); sys.exit(1)
    local_id = r["id"]
    record = await core.export_record({"id": local_id})
    print(f"  ✓ stored local id={local_id[:8]}, vector dim={len(record['vector'])}")

    print("\nSTEP 4: POST /memory/put (simulating another peer sending us this memory)")
    put = post_put(record, origin_node=SIMULATED_PEER)
    passed.append(harness.check("PUT HTTP 200", put["status_code"] == 200))
    passed.append(harness.check("PUT status == 'stored'",
                                put["body"].get("status") == "stored"))
    new_id = put["body"].get("id")
    passed.append(harness.check("PUT returns a new id (≠ originator's local id)",
                                bool(new_id) and new_id != local_id))

    print("\nSTEP 5: GET /memory/get and verify byte-identical integrity")
    got = get_by_id(new_id)
    passed.append(harness.check("GET HTTP 200 with count == 1",
                                got["status_code"] == 200 and got["body"]["count"] == 1))
    received = got["body"]["memories"][0]
    passed.append(harness.check("content matches byte-identically",
                                received["content"] == record["content"]))
    passed.append(harness.check("content_hash matches",
                                received["content_hash"] == record["content_hash"]))
    passed.append(harness.check("content_hash recomputes correctly",
                                received["content_hash"] == core.content_hash(received["content"])))
    passed.append(harness.check("vector length == 768",
                                isinstance(received["vector"], list) and len(received["vector"]) == 768))
    passed.append(harness.check("vector matches byte-identically",
                                received["vector"] == record["vector"]))
    passed.append(harness.check("type preserved", received["type"] == record["type"]))
    passed.append(harness.check("tags preserved", received["tags"] == record["tags"]))
    passed.append(harness.check("importance preserved",
                                received["importance"] == record["importance"]))
    passed.append(harness.check("origin_node preserved",
                                received["origin_node"] == SIMULATED_PEER))
    passed.append(harness.check("submitted_at present",
                                bool(received.get("submitted_at"))))
    passed.append(harness.check("received_at present",
                                bool(received.get("received_at"))))
    passed.append(harness.check("group_name attached",
                                received["group_name"] == GROUP_NAME))

    print("\nSTEP 6: re-POST same content → expect 'duplicate'")
    put2 = post_put(record, origin_node=SIMULATED_PEER)
    passed.append(harness.check("re-PUT status == 'duplicate'",
                                put2["body"].get("status") == "duplicate"))
    passed.append(harness.check("duplicate returns the same id",
                                put2["body"].get("id") == new_id))

    print("\nSTEP 7: negative — content_hash mismatch should be rejected")
    bad = post_put(record, origin_node=SIMULATED_PEER,
                   content_hash_override="deadbeef" * 2)
    passed.append(harness.check("tampered content_hash → HTTP 400",
                                bad["status_code"] == 400))
    passed.append(harness.check("error message mentions content_hash mismatch",
                                "content_hash mismatch" in bad["body"].get("error", "")))


def main():
    print("=" * 70)
    print("Constellation promotion test — one daemon, simulated peer push")
    print("=" * 70)

    procs = []
    with tempfile.TemporaryDirectory(prefix="constellation-promo-") as tdir:
        tmp = pathlib.Path(tdir)
        logs = tmp / "logs"; logs.mkdir()
        try:
            print("\nSTEP 1: start Qdrant + Constellation")
            (tmp / "qdrant").mkdir()
            procs.append(harness.start_qdrant(RECEIVER["qdrant_port"],
                                              tmp / "qdrant",
                                              logs / "qdrant.log"))
            harness.init_cowork_memories(RECEIVER["qdrant_port"])
            print(f"  ✓ Qdrant on :{RECEIVER['qdrant_port']}, collection initialized")

            cfg = tmp / "config.json"
            harness.write_constellation_config(
                cfg,
                node_name=RECEIVER["node_name"],
                peer_port=RECEIVER["peer_port"],
                gateway_port=RECEIVER["gateway_port"],
                qdrant_port=RECEIVER["qdrant_port"],
                state_dir=tmp / "state",
                group_name=GROUP_NAME,
                peers=[],
            )
            procs.append(harness.start_constellation(cfg, logs / "constellation.log"))
            if not harness.wait_health(RECEIVER["peer_port"]):
                cl = logs / "constellation.log"
                if cl.exists():
                    print(f"--- constellation log ---\n{cl.read_text()[-2000:]}",
                          file=sys.stderr)
                raise RuntimeError("Constellation failed to come up")
            print(f"  ✓ Constellation on peer :{RECEIVER['peer_port']}, "
                  f"gateway :{RECEIVER['gateway_port']}")

            print("\nSTEP 2: smoke-test /peers/self")
            r = httpx.get(f"http://127.0.0.1:{RECEIVER['peer_port']}/peers/self",
                          timeout=5.0).json()
            passed = []
            passed.append(harness.check("/peers/self node_name matches",
                                        r["node_name"] == RECEIVER["node_name"]))
            passed.append(harness.check("/peers/self lists our group",
                                        GROUP_NAME in r["memberships"]))

            asyncio.run(run_tests(passed))

            ok, total = sum(passed), len(passed)
            print(f"\n  {ok}/{total} invariants passed")
            if ok != total:
                print("\n✗ Promotion verification FAILED", file=sys.stderr)
                sys.exit(1)
            print("\n" + "=" * 70)
            print("✓ Promotion verification PASSED")
            print("=" * 70)

        finally:
            for p in reversed(procs):
                harness.stop_proc(p)


if __name__ == "__main__":
    main()
