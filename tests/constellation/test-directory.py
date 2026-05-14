#!/usr/bin/env python3
"""
Constellation directory test: /peers and /peers/self endpoint behavior.

Verifies the peer-aggregation logic by POSTing several /memory/put bodies
with varying `origin_node` values to one Constellation daemon, then checking
that /peers groups them correctly (submission_count, first_seen/last_seen,
sort order). Also tests the negative cases: unknown group → 403, missing
group_name → 400.

Self-contained — one Qdrant + one Constellation daemon. Multiple "peers"
are simulated by varying the `origin_node` field on submitted memories.

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/constellation/test-directory.py
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


GROUP_NAME = "test-directory@e2e"
RECEIVER   = {"node_name": "directory-receiver", "qdrant_port": 6743,
              "peer_port": 7743, "gateway_port": 7744}


def use_qdrant(port):
    url = f"http://127.0.0.1:{port}"
    core.QDRANT_URL = url
    core.qdrant = QdrantClient(url=url, timeout=10)


def iso_now():
    return datetime.now(timezone.utc).isoformat()


async def make_record(content, type_="decision"):
    """Create an exportable record. We delete the local copy afterward so the
    subsequent /memory/put posting it back exercises the 'fresh receive' path
    (otherwise v0.4's global content_hash dedup would merge instead of store)."""
    use_qdrant(RECEIVER["qdrant_port"])
    r = await core.store_memory({
        "content": content, "type": type_,
        "tags": ["e2e", "directory-test"], "project": "directory-test", "importance": 3,
    })
    if r.get("status") != "stored":
        raise RuntimeError(f"store failed: {r}")
    rec = await core.export_record({"id": r["id"]})
    await core.delete_memory({"id": r["id"]})
    return rec


def post_put(record, *, origin_node):
    """POST /memory/put with the given origin_node."""
    body = {
        "content":      record["content"],
        "content_hash": record["content_hash"],
        "vector":       record["vector"],
        "type":         record["type"],
        "tags":         record["tags"],
        "project":      record.get("project", ""),
        "importance":   record["importance"],
        "groups":       [GROUP_NAME],
        "origin_node":  origin_node,
        "submitted_at": iso_now(),
    }
    return httpx.post(f"http://127.0.0.1:{RECEIVER['peer_port']}/memory/put",
                      json=body, timeout=10.0)


def get_peers(group_name=GROUP_NAME):
    return httpx.get(f"http://127.0.0.1:{RECEIVER['peer_port']}/peers",
                     params={"group_name": group_name}, timeout=5.0)


async def run_tests(passed):
    print("\nSTEP 3: GET /peers/self")
    r = httpx.get(f"http://127.0.0.1:{RECEIVER['peer_port']}/peers/self",
                  timeout=5).json()
    print(f"  response: {json.dumps(r, indent=2)}")
    passed.append(harness.check("self.node_name matches",
                                r["node_name"] == RECEIVER["node_name"]))
    passed.append(harness.check("self has version field", "version" in r))
    passed.append(harness.check("self has listen_address", "listen_address" in r))
    passed.append(harness.check("self lists our group as a flat name",
                                GROUP_NAME in r["memberships"]))

    print("\nSTEP 4: GET /peers with no submissions yet → empty")
    r = get_peers().json()
    print(f"  response: {json.dumps(r, indent=2)}")
    passed.append(harness.check("empty group: count == 0", r["count"] == 0))
    passed.append(harness.check("empty group: peers == []", r["peers"] == []))
    passed.append(harness.check("responding_node set",
                                r["responding_node"] == RECEIVER["node_name"]))

    print("\nSTEP 5: simulate peer-alpha pushing 1 memory")
    rec_a1 = await make_record(
        "Mesh size at MVP is capped around 10 peers per group.")
    post_put(rec_a1, origin_node="peer-alpha")
    r = get_peers().json()
    passed.append(harness.check("after 1 PUT: count == 1", r["count"] == 1))
    if r["count"] >= 1:
        alpha = r["peers"][0]
        passed.append(harness.check("peer-alpha appears",
                                    alpha["node_name"] == "peer-alpha"))
        passed.append(harness.check("submission_count == 1",
                                    alpha["submission_count"] == 1))
        passed.append(harness.check("first_seen and last_seen match (single PUT)",
                                    alpha["first_seen"] == alpha["last_seen"]))

    print("\nSTEP 6: simulate peer-alpha pushing 2 more memories")
    rec_a2 = await make_record(
        "Content_hash is SHA256(content.strip().lower()).hexdigest()[:16].",
        type_="fact")
    rec_a3 = await make_record(
        "Vectors are 768-dim from nomic-embed-text.", type_="fact")
    post_put(rec_a2, origin_node="peer-alpha")
    post_put(rec_a3, origin_node="peer-alpha")
    r = get_peers().json()
    alpha_entry = next((p for p in r["peers"] if p["node_name"] == "peer-alpha"), None)
    passed.append(harness.check("peer-alpha submission_count == 3",
                                alpha_entry and alpha_entry["submission_count"] == 3))
    passed.append(harness.check("last_seen > first_seen after multi PUT",
                                alpha_entry
                                and alpha_entry["last_seen"] > alpha_entry["first_seen"]))

    print("\nSTEP 7: simulate peer-beta pushing 1 memory → peer count == 2")
    rec_b = await make_record(
        "Cursor is keyed on submitted_at for cross-peer comparability.",
        type_="decision")
    post_put(rec_b, origin_node="peer-beta")
    r = get_peers().json()
    print(f"  response: {json.dumps(r, indent=2)}")
    passed.append(harness.check("after peer-beta PUT: count == 2", r["count"] == 2))
    passed.append(harness.check("peer-beta appears",
                                any(p["node_name"] == "peer-beta" for p in r["peers"])))
    passed.append(harness.check("peer-alpha still shows submission_count == 3",
                                any(p["node_name"] == "peer-alpha"
                                    and p["submission_count"] == 3
                                    for p in r["peers"])))
    passed.append(harness.check("peers sorted by last_seen descending",
                                r["peers"][0]["last_seen"] >= r["peers"][1]["last_seen"]))

    print("\nSTEP 8: negative — /peers for unknown group → 403")
    r = httpx.get(f"http://127.0.0.1:{RECEIVER['peer_port']}/peers",
                  params={"group_name": "bogus@group"}, timeout=5)
    passed.append(harness.check("unknown group returns 403", r.status_code == 403))

    print("\nSTEP 9: negative — /peers without group_name → 400")
    r = httpx.get(f"http://127.0.0.1:{RECEIVER['peer_port']}/peers", timeout=5)
    passed.append(harness.check("missing group_name returns 400", r.status_code == 400))


def main():
    print("=" * 70)
    print("Constellation directory test — /peers and /peers/self")
    print("=" * 70)

    procs = []
    with tempfile.TemporaryDirectory(prefix="constellation-dir-") as tdir:
        tmp = pathlib.Path(tdir)
        logs = tmp / "logs"; logs.mkdir()
        try:
            print("\nSTEP 1: start Qdrant + Constellation")
            (tmp / "qdrant").mkdir()
            procs.append(harness.start_qdrant(RECEIVER["qdrant_port"],
                                              tmp / "qdrant",
                                              logs / "qdrant.log"))
            harness.init_cowork_memories(RECEIVER["qdrant_port"])
            print(f"  ✓ Qdrant on :{RECEIVER['qdrant_port']}")

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
            print(f"  ✓ Constellation peer :{RECEIVER['peer_port']}, "
                  f"gateway :{RECEIVER['gateway_port']}")

            passed = []
            print("\nSTEP 2: smoke-test /health")
            r = httpx.get(f"http://127.0.0.1:{RECEIVER['peer_port']}/health",
                          timeout=5).json()
            passed.append(harness.check("/health ok=True",
                                        r.get("ok") is True))

            asyncio.run(run_tests(passed))

            ok, total = sum(passed), len(passed)
            print(f"\n  {ok}/{total} invariants passed")
            if ok != total:
                print("\n✗ Directory verification FAILED", file=sys.stderr)
                sys.exit(1)
            print("\n" + "=" * 70)
            print("✓ Directory verification PASSED")
            print("=" * 70)

        finally:
            for p in reversed(procs):
                harness.stop_proc(p)


if __name__ == "__main__":
    main()
