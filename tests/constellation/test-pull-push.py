#!/usr/bin/env python3
"""
End-to-end test for v0.4 `group_pull` and `group_push`: spins up two Qdrants
and two Constellation daemons, each pointed at the other as a peer, then
exercises the gateway endpoints directly via HTTP.

Verifies:
  - push: send a local memory from A's gateway to B; B inserts the entry
          with the target group in its `groups` list
  - push idempotency: re-push of same memory results in delivery=duplicate
  - pull dedup: pull when B already has the entry returns zero stored,
                merge counts allowed
  - pull privacy: A's memories tagged personal-only are NOT returned via pull
                  for the shared group
  - pull catch-up: push from A, drop on B, pull — B recovers the entry
  - push-time filter: A pushes a memory tagged [personal, shared]; B only
                      sees `groups=[shared]` (personal stripped on the wire)

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


def count_in_group(port, group_name):
    """Count entries in port's Qdrant whose `groups` array contains group_name."""
    client = QdrantClient(url=f"http://127.0.0.1:{port}", timeout=10)
    pts, _ = client.scroll(
        collection_name=core.COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="groups", match=MatchValue(value=group_name)),
        ]),
        limit=100, with_payload=False, with_vectors=False,
    )
    return len(pts)


async def insert_local(qdrant_port, content, type_="decision", groups=None):
    """Insert a fresh local memory via core.store_memory on the given Qdrant."""
    use_qdrant(qdrant_port)
    args = {
        "content": content, "type": type_,
        "tags": ["e2e"], "project": "pull-push-test", "importance": 4,
    }
    if groups is not None:
        args["groups"] = groups
    r = await core.store_memory(args)
    if r.get("status") != "stored":
        raise RuntimeError(f"store failed: {r}")
    return r["id"]


def first_delivery(push_body):
    """Pluck the first delivery row from a push response (one memory pushed)."""
    deliveries = push_body.get("deliveries") or []
    return deliveries[0] if deliveries else None


async def run_tests(passed):
    print("\nSTEP 4: insert a memory tagged [GROUP] on peer-a and push to group")
    a_id_1 = await insert_local(
        PEER_A["qdrant_port"],
        "gRPC for internal RPC; HTTP/JSON for public APIs.",
        groups=[GROUP_NAME],
    )

    r = httpx.post(f"http://127.0.0.1:{PEER_A['gateway_port']}/push",
                   json={"group": GROUP_NAME, "memory_ids": [a_id_1]},
                   timeout=20.0)
    passed.append(harness.check("push HTTP 200", r.status_code == 200))
    body = r.json()
    passed.append(harness.check("push response has peers list",
                                isinstance(body.get("peers"), list)))
    b_entry = next((p for p in body["peers"] if p["node_name"] == PEER_B["node_name"]), None)
    passed.append(harness.check("push: peer-b responsive",
                                b_entry and b_entry["status"] == "responsive",
                                f"got {b_entry}"))
    deliv = b_entry and first_delivery(b_entry)
    passed.append(harness.check("push: peer-b delivery == stored",
                                deliv and deliv.get("status") == "stored",
                                f"got {deliv}"))

    passed.append(harness.check("peer-b has 1 entry tagged GROUP after push",
                                count_in_group(PEER_B["qdrant_port"], GROUP_NAME) == 1))
    passed.append(harness.check("peer-a's entry still tagged GROUP",
                                count_in_group(PEER_A["qdrant_port"], GROUP_NAME) == 1))

    print("\nSTEP 5: push the same memory again → delivery=duplicate on peer-b")
    r = httpx.post(f"http://127.0.0.1:{PEER_A['gateway_port']}/push",
                   json={"group": GROUP_NAME, "memory_ids": [a_id_1]},
                   timeout=20.0)
    b_entry = next((p for p in r.json()["peers"] if p["node_name"] == PEER_B["node_name"]), None)
    deliv = b_entry and first_delivery(b_entry)
    passed.append(harness.check("push idempotent: delivery == duplicate",
                                deliv and deliv.get("status") == "duplicate"))

    print("\nSTEP 6: pull on peer-b → zero new entry_ids (already has it)")
    r = httpx.post(f"http://127.0.0.1:{PEER_B['gateway_port']}/pull",
                   json={}, timeout=20.0)
    passed.append(harness.check("pull HTTP 200", r.status_code == 200))
    a_entry = next((p for p in r.json()["peers"] if p["node_name"] == PEER_A["node_name"]), None)
    passed.append(harness.check("pull: peer-a responsive",
                                a_entry and a_entry["status"] == "responsive"))
    passed.append(harness.check("pull returned zero new entry_ids (dedup)",
                                a_entry and a_entry.get("entry_ids") == []))

    print("\nSTEP 7: insert a PERSONAL-only memory on A; pull on B → not visible")
    a_id_2 = await insert_local(
        PEER_A["qdrant_port"],
        "Quantum mechanics describes wave-particle duality.",
        type_="fact", groups=["personal"],
    )
    r = httpx.post(f"http://127.0.0.1:{PEER_B['gateway_port']}/pull",
                   json={}, timeout=20.0)
    a_entry = next((p for p in r.json()["peers"] if p["node_name"] == PEER_A["node_name"]), None)
    passed.append(harness.check("pull skips personal-only memory (privacy)",
                                a_entry and a_entry.get("entry_ids") == []))

    print("\nSTEP 8: push a third memory; drop on B; pull on B → recovers it")
    a_id_3 = await insert_local(
        PEER_A["qdrant_port"],
        "JSON Schema is the canonical format for our API contracts.",
        groups=[GROUP_NAME],
    )
    use_qdrant(PEER_A["qdrant_port"])
    record_3 = await core.export_record({"id": a_id_3})
    httpx.post(f"http://127.0.0.1:{PEER_A['gateway_port']}/push",
               json={"group": GROUP_NAME, "memory_ids": [a_id_3]}, timeout=20.0)

    # Simulate B missing the entry: delete by content_hash before pull
    use_qdrant(PEER_B["qdrant_port"])
    client_b = QdrantClient(url=f"http://127.0.0.1:{PEER_B['qdrant_port']}", timeout=10)
    pts, _ = client_b.scroll(
        collection_name=core.COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="content_hash",
                           match=MatchValue(value=record_3["content_hash"])),
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
    passed.append(harness.check("peer-b has 2 entries tagged GROUP after pull",
                                count_in_group(PEER_B["qdrant_port"], GROUP_NAME) == 2))

    print("\nSTEP 9: push-time group filter — personal tag stripped on the wire")
    # Insert a memory tagged [personal, GROUP] on A; push to GROUP; verify B
    # sees ONLY groups=[GROUP] on the receiving side (personal never crosses).
    a_id_4 = await insert_local(
        PEER_A["qdrant_port"],
        "A multi-group memory: personal AND shared.",
        groups=["personal", GROUP_NAME],
    )
    httpx.post(f"http://127.0.0.1:{PEER_A['gateway_port']}/push",
               json={"group": GROUP_NAME, "memory_ids": [a_id_4]}, timeout=20.0)
    # Inspect B's record for that content_hash
    use_qdrant(PEER_A["qdrant_port"])
    record_4 = await core.export_record({"id": a_id_4})
    client_b = QdrantClient(url=f"http://127.0.0.1:{PEER_B['qdrant_port']}", timeout=10)
    pts, _ = client_b.scroll(
        collection_name=core.COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="content_hash",
                           match=MatchValue(value=record_4["content_hash"])),
        ]),
        limit=1, with_payload=True,
    )
    b_groups = (pts[0].payload or {}).get("groups", []) if pts else []
    passed.append(harness.check(
        "B's copy carries only the shared group (personal stripped)",
        b_groups == [GROUP_NAME], f"got groups={b_groups}",
    ))


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
