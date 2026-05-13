#!/usr/bin/env python3
"""
End-to-end test for `group_pull` and `group_push`: spins up two Qdrants and
two Constellation daemons, each pointed at the other as a peer, then exercises
the gateway endpoints directly via HTTP.

Verifies:
  - push: send a local memory from A to B; B inserts as source=group;
          A's local entry gets augmented with group_name + submitted_at
  - push idempotency: re-push returns delivery=duplicate
  - pull dedup: pull when B already has the entry returns zero new
  - pull privacy: A's memories without group_name are not returned via pull
  - pull catch-up: push from A, drop on B, pull — B recovers the entry

Self-contained — uses tests/lib/harness.py to spin up Qdrant + Constellation.

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/constellation/test-pull-push.py
"""
import asyncio
import pathlib
import subprocess
import sys
import tempfile

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "lib"))
import core      # noqa: E402
import harness   # noqa: E402


GROUP_NAME = "test-pull-push@e2e"

PEER_A = {"node_name": "peer-a", "qdrant_port": 6833, "peer_port": 7833, "gateway_port": 7834}
PEER_B = {"node_name": "peer-b", "qdrant_port": 6933, "peer_port": 7933, "gateway_port": 7934}


def use_qdrant(port):
    """Rebind core.qdrant to a specific port."""
    url = f"http://127.0.0.1:{port}"
    core.QDRANT_URL = url
    core.qdrant = QdrantClient(url=url, timeout=10)


def count_in_group(port, group_name, *, source=None):
    """Count entries in port's Qdrant matching group_name (+ optional source)."""
    client = QdrantClient(url=f"http://127.0.0.1:{port}", timeout=10)
    must = [FieldCondition(key="group_name", match=MatchValue(value=group_name))]
    if source:
        must.append(FieldCondition(key="source", match=MatchValue(value=source)))
    pts, _ = client.scroll(
        collection_name=core.COLLECTION,
        scroll_filter=Filter(must=must),
        limit=100, with_payload=False, with_vectors=False,
    )
    return len(pts)


async def insert_local(qdrant_port, content, type_="decision"):
    """Insert a fresh local memory via core.store_memory on the given Qdrant."""
    use_qdrant(qdrant_port)
    r = await core.store_memory({
        "content": content, "type": type_,
        "tags": ["e2e"], "project": "pull-push-test", "importance": 4,
    })
    if r.get("status") != "stored":
        raise RuntimeError(f"store failed: {r}")
    return r["id"]


async def run_tests(passed):
    print("\nSTEP 4: insert a memory locally on peer-a and push to group")
    a_id_1 = await insert_local(PEER_A["qdrant_port"],
                                "gRPC for internal RPC; HTTP/JSON for public APIs.")
    use_qdrant(PEER_A["qdrant_port"])
    record = await core.export_record({"id": a_id_1})

    r = httpx.post(f"http://127.0.0.1:{PEER_A['gateway_port']}/push",
                   json={"record": record}, timeout=20.0)
    passed.append(harness.check("push HTTP 200", r.status_code == 200))
    body = r.json()
    passed.append(harness.check("push response has peers list",
                                isinstance(body.get("peers"), list)))
    b_entry = next((p for p in body["peers"] if p["node_name"] == PEER_B["node_name"]), None)
    passed.append(harness.check("push: peer-b responsive",
                                b_entry and b_entry["status"] == "responsive",
                                f"got {b_entry}"))
    passed.append(harness.check("push: peer-b delivery == stored",
                                b_entry and b_entry.get("delivery") == "stored"))

    passed.append(harness.check("peer-b has 1 source=group entry after push",
                                count_in_group(PEER_B["qdrant_port"], GROUP_NAME,
                                               source="group") == 1))
    passed.append(harness.check("peer-a's local entry now carries group_name",
                                count_in_group(PEER_A["qdrant_port"], GROUP_NAME,
                                               source="local") == 1))

    print("\nSTEP 5: push the same memory again → delivery=duplicate on peer-b")
    r = httpx.post(f"http://127.0.0.1:{PEER_A['gateway_port']}/push",
                   json={"record": record}, timeout=20.0)
    b_entry = next((p for p in r.json()["peers"] if p["node_name"] == PEER_B["node_name"]), None)
    passed.append(harness.check("push idempotent: delivery == duplicate",
                                b_entry and b_entry.get("delivery") == "duplicate"))

    print("\nSTEP 6: pull on peer-b → zero new entries (already has it)")
    r = httpx.post(f"http://127.0.0.1:{PEER_B['gateway_port']}/pull",
                   json={}, timeout=20.0)
    passed.append(harness.check("pull HTTP 200", r.status_code == 200))
    a_entry = next((p for p in r.json()["peers"] if p["node_name"] == PEER_A["node_name"]), None)
    passed.append(harness.check("pull: peer-a responsive",
                                a_entry and a_entry["status"] == "responsive"))
    passed.append(harness.check("pull returned zero new entry_ids (dedup)",
                                a_entry and a_entry.get("entry_ids") == []))

    print("\nSTEP 7: insert a SECOND memory on A but don't push; pull on B")
    a_id_2 = await insert_local(PEER_A["qdrant_port"],
                                 "Quantum mechanics describes wave-particle duality.",
                                 type_="fact")
    r = httpx.post(f"http://127.0.0.1:{PEER_B['gateway_port']}/pull",
                   json={}, timeout=20.0)
    a_entry = next((p for p in r.json()["peers"] if p["node_name"] == PEER_A["node_name"]), None)
    passed.append(harness.check("pull skips A's non-pushed memory (no group_name yet)",
                                a_entry and a_entry.get("entry_ids") == []))

    print("\nSTEP 8: push the second memory; drop on B; pull on B → recovers it")
    use_qdrant(PEER_A["qdrant_port"])
    record_2 = await core.export_record({"id": a_id_2})
    httpx.post(f"http://127.0.0.1:{PEER_A['gateway_port']}/push",
               json={"record": record_2}, timeout=20.0)

    # Simulate B missing the entry: delete it locally before pull
    use_qdrant(PEER_B["qdrant_port"])
    client_b = QdrantClient(url=f"http://127.0.0.1:{PEER_B['qdrant_port']}", timeout=10)
    pts, _ = client_b.scroll(
        collection_name=core.COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="content_hash",
                           match=MatchValue(value=record_2["content_hash"])),
            FieldCondition(key="group_name", match=MatchValue(value=GROUP_NAME)),
        ]),
        limit=10, with_payload=False,
    )
    for p in pts:
        client_b.delete(collection_name=core.COLLECTION, points_selector=[p.id])

    r = httpx.post(f"http://127.0.0.1:{PEER_B['gateway_port']}/pull",
                   json={}, timeout=20.0)
    a_entry = next((p for p in r.json()["peers"] if p["node_name"] == PEER_A["node_name"]), None)
    passed.append(harness.check("pull recovers the dropped entry",
                                a_entry and len(a_entry.get("entry_ids", [])) == 1,
                                f"got {a_entry}"))
    passed.append(harness.check("peer-b has 2 source=group entries",
                                count_in_group(PEER_B["qdrant_port"], GROUP_NAME,
                                               source="group") == 2))


def main():
    print("=" * 70)
    print("Constellation pull/push end-to-end — two daemons, real HTTP")
    print("=" * 70)

    if not harness.QDRANT_BIN.exists() or not harness.CONSTELLATION.exists():
        print(f"✗ missing dependency: qdrant={harness.QDRANT_BIN}, "
              f"constellation={harness.CONSTELLATION}", file=sys.stderr)
        sys.exit(1)

    procs = []
    with tempfile.TemporaryDirectory(prefix="constellation-e2e-") as tdir:
        tmp = pathlib.Path(tdir)
        logs = tmp / "logs"; logs.mkdir()
        try:
            print("\nSTEP 1: start two Qdrants")
            (tmp / "qa").mkdir(); (tmp / "qb").mkdir()
            procs.append(harness.start_qdrant(PEER_A["qdrant_port"], tmp / "qa",
                                              logs / "qdrant-a.log"))
            procs.append(harness.start_qdrant(PEER_B["qdrant_port"], tmp / "qb",
                                              logs / "qdrant-b.log"))
            print(f"  ✓ Qdrant peer-a on :{PEER_A['qdrant_port']}")
            print(f"  ✓ Qdrant peer-b on :{PEER_B['qdrant_port']}")
            harness.init_cowork_memories(PEER_A["qdrant_port"])
            harness.init_cowork_memories(PEER_B["qdrant_port"])
            print("  ✓ collections initialized")

            print("\nSTEP 2: start two Constellation daemons")
            cfg_a = tmp / "config-a.json"; cfg_b = tmp / "config-b.json"
            harness.write_constellation_config(
                cfg_a,
                node_name=PEER_A["node_name"],
                peer_port=PEER_A["peer_port"], gateway_port=PEER_A["gateway_port"],
                qdrant_port=PEER_A["qdrant_port"],
                state_dir=tmp / "state-a", group_name=GROUP_NAME,
                peers=[{"node_name": PEER_B["node_name"],
                        "endpoint": f"http://127.0.0.1:{PEER_B['peer_port']}"}],
            )
            harness.write_constellation_config(
                cfg_b,
                node_name=PEER_B["node_name"],
                peer_port=PEER_B["peer_port"], gateway_port=PEER_B["gateway_port"],
                qdrant_port=PEER_B["qdrant_port"],
                state_dir=tmp / "state-b", group_name=GROUP_NAME,
                peers=[{"node_name": PEER_A["node_name"],
                        "endpoint": f"http://127.0.0.1:{PEER_A['peer_port']}"}],
            )
            procs.append(harness.start_constellation(cfg_a, logs / "const-a.log"))
            procs.append(harness.start_constellation(cfg_b, logs / "const-b.log"))
            if not (harness.wait_health(PEER_A["peer_port"])
                    and harness.wait_health(PEER_B["peer_port"])):
                for fn in ("const-a.log", "const-b.log"):
                    p = logs / fn
                    if p.exists():
                        print(f"--- {fn} ---", file=sys.stderr)
                        print(p.read_text()[-2000:], file=sys.stderr)
                raise RuntimeError("Constellation daemons failed to come up")
            print(f"  ✓ Constellation peer-a: peer :{PEER_A['peer_port']}, "
                  f"gateway :{PEER_A['gateway_port']}")
            print(f"  ✓ Constellation peer-b: peer :{PEER_B['peer_port']}, "
                  f"gateway :{PEER_B['gateway_port']}")

            print("\nSTEP 3: smoke-test gateway endpoints")
            passed = []
            r = httpx.post(f"http://127.0.0.1:{PEER_A['gateway_port']}/pull",
                           json={}, timeout=5.0)
            passed.append(harness.check("A's gateway /pull returns 200",
                                        r.status_code == 200))
            passed.append(harness.check("A's gateway /pull returns peers list",
                                        "peers" in r.json()))

            asyncio.run(run_tests(passed))

            ok, total = sum(passed), len(passed)
            print(f"\n  {ok}/{total} invariants passed")
            if ok != total:
                print("\n✗ pull/push end-to-end FAILED", file=sys.stderr)
                sys.exit(1)
            print("\n" + "=" * 70)
            print("✓ pull/push end-to-end PASSED")
            print("=" * 70)

        finally:
            for p in reversed(procs):
                harness.stop_proc(p)


if __name__ == "__main__":
    main()
