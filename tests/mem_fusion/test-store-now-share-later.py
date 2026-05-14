#!/usr/bin/env python3
"""
v0.4 store-now-share-later workflow test.

Walks the canonical user flow from the v0.4 design doc:

  T1.  Store 4 memories with default groups=[personal] — local only.
  T2.  "Share those with engineering"
         → add_groups(ids, [engineering]) → group_push(engineering, ids)
  T3.  "Also share with product"
         → add_groups(ids, [product])     → group_push(product, ids)

Critical invariants verified:
  - After T1, no peer (engineering or product) has any of the memories.
  - After T2, the engineering peer has all 4 memories, tagged groups=[engineering].
              The product peer still has nothing (scope rule).
  - After T3, the product peer has all 4 memories, tagged groups=[product].
              The engineering peer is NOT re-contacted — still 4 entries from T2.
  - Origin's local entries carry the full union groups=[personal, engineering, product].
  - Push-time group filter: receivers never see the `personal` tag (which would
    be the origin's personal group, semantically meaningless to a teammate).

Three peers:
  - alice  (origin): memberships=[personal, engineering:[bob], product:[carol]]
  - bob    (eng):    memberships=[engineering]      ← receives engineering pushes
  - carol  (prod):   memberships=[product]          ← receives product pushes

Six subprocesses: 3 Qdrants + 3 Constellations + 3 mem-fusion MCP servers.

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/mem_fusion/test-store-now-share-later.py
"""
import pathlib
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "lib"))
import core      # noqa: E402
import harness   # noqa: E402

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue


GROUP_ENG  = "engineering@e2e"
GROUP_PROD = "product@e2e"

ALICE = {"node_name": "alice",
         "qdrant_port": 6473, "peer_port": 7473, "gateway_port": 7474}
BOB   = {"node_name": "bob",
         "qdrant_port": 6483, "peer_port": 7483, "gateway_port": 7484}
CAROL = {"node_name": "carol",
         "qdrant_port": 6493, "peer_port": 7493, "gateway_port": 7494}

MEMORIES = [
    {"content": "We picked gRPC for internal RPC; HTTP/JSON for public APIs.",
     "type": "decision", "tags": ["architecture", "rpc"]},
    {"content": "Postgres 16 with logical replication is the auth-service store.",
     "type": "decision", "tags": ["database"]},
    {"content": "JWT clock-skew bug: leeway 30s in the validation chain.",
     "type": "error", "tags": ["auth", "jwt"]},
    {"content": "Auth migration targets late-Q2; product owner: Mitch.",
     "type": "context", "tags": ["migration"]},
]


def entries_tagged(qdrant_port: int, group: str) -> list[dict]:
    """Return Qdrant entries on `qdrant_port` whose `groups` array contains `group`."""
    client = QdrantClient(url=f"http://127.0.0.1:{qdrant_port}", timeout=10)
    pts, _ = client.scroll(
        collection_name=core.COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="groups", match=MatchValue(value=group)),
        ]),
        limit=100, with_payload=True, with_vectors=False,
    )
    return [{"id": str(p.id), "content": (p.payload or {}).get("content", ""),
             "groups": (p.payload or {}).get("groups", [])}
            for p in pts]


def entry_groups_on(qdrant_port: int, memory_id: str) -> list[str]:
    client = QdrantClient(url=f"http://127.0.0.1:{qdrant_port}", timeout=10)
    pts = client.retrieve(collection_name=core.COLLECTION, ids=[memory_id],
                          with_payload=True, with_vectors=False)
    if not pts:
        return []
    return (pts[0].payload or {}).get("groups", [])


def run_tests(mcp_alice: harness.MCPClient,
              mcp_bob:   harness.MCPClient,
              mcp_carol: harness.MCPClient,
              passed: list) -> None:
    print("\nSTEP 3: handshake all three mem-fusion MCP servers")
    for mcp, name in [(mcp_alice, "alice"), (mcp_bob, "bob"), (mcp_carol, "carol")]:
        harness.mcp_initialize(mcp, client_name=f"share-later-{name}")
    passed.append(harness.check("3 mem-fusion subprocesses initialized", True))

    # ── T1: Alice stores 4 memories with default groups=[personal] ──────
    print("\nT1: Alice stores 4 memories with default groups=[personal] (local-only)")
    stored_ids = []
    for mem in MEMORIES:
        r = mcp_alice.call_tool("store_memory", {
            **mem, "project": "share-later-test", "importance": 4,
            # groups omitted — defaults to ["personal"]
        })
        if r.get("status") != "stored":
            print(f"  ✗ store failed: {r}", file=sys.stderr); sys.exit(1)
        stored_ids.append(r["id"])
    passed.append(harness.check(f"alice stored 4 memories",
                                len(stored_ids) == 4))
    passed.append(harness.check(
        "all 4 default to groups=[personal]",
        all(entry_groups_on(ALICE["qdrant_port"], mid) == ["personal"]
            for mid in stored_ids),
        "one or more memories has unexpected groups",
    ))

    print("  → bob (engineering peer) should have 0 entries tagged engineering")
    passed.append(harness.check("bob has no engineering entries pre-share",
                                len(entries_tagged(BOB["qdrant_port"], GROUP_ENG)) == 0))
    print("  → carol (product peer) should have 0 entries tagged product")
    passed.append(harness.check("carol has no product entries pre-share",
                                len(entries_tagged(CAROL["qdrant_port"], GROUP_PROD)) == 0))

    # ── T2: "Share those with engineering" ──────────────────────────────
    print("\nT2: 'Share those with engineering' — add_groups + targeted push to engineering")
    add = mcp_alice.call_tool("add_groups", {
        "memory_ids": stored_ids, "groups": [GROUP_ENG],
    })
    passed.append(harness.check("add_groups: 4 updated, 0 no_op",
                                len(add.get("updated", [])) == 4
                                and not add.get("no_op")
                                and not add.get("errors"),
                                f"got {add}"))

    push_eng = mcp_alice.call_tool("group_push", {
        "group": GROUP_ENG, "memory_ids": stored_ids,
    })
    bob_resp = next((p for p in push_eng.get("peers", [])
                     if p["node_name"] == BOB["node_name"]), None)
    passed.append(harness.check("push to engineering: bob responsive",
                                bob_resp and bob_resp["status"] == "responsive",
                                f"got {bob_resp}"))
    passed.append(harness.check("push to engineering: 4 deliveries to bob",
                                bob_resp and len(bob_resp.get("deliveries", [])) == 4))
    passed.append(harness.check(
        "push to engineering: all 4 stored on bob",
        bob_resp and all(d.get("status") == "stored"
                        for d in bob_resp.get("deliveries", [])),
        f"deliveries: {bob_resp.get('deliveries') if bob_resp else None}",
    ))
    # Scope: carol must not appear in engineering push response (different group).
    carol_in_eng_push = any(p["node_name"] == CAROL["node_name"]
                            for p in push_eng.get("peers", []))
    passed.append(harness.check("scope: carol NOT in engineering push response",
                                not carol_in_eng_push))

    # State after T2
    alice_groups_post_T2 = entry_groups_on(ALICE["qdrant_port"], stored_ids[0])
    passed.append(harness.check(
        "after T2, alice's memory tagged [personal, engineering]",
        set(alice_groups_post_T2) == {"personal", GROUP_ENG},
        f"got {alice_groups_post_T2}",
    ))
    bob_entries_post_T2 = entries_tagged(BOB["qdrant_port"], GROUP_ENG)
    passed.append(harness.check("after T2, bob has 4 engineering entries",
                                len(bob_entries_post_T2) == 4,
                                f"got {len(bob_entries_post_T2)}"))
    passed.append(harness.check(
        "after T2, bob's entries tagged ONLY [engineering] (no personal leak)",
        all(e["groups"] == [GROUP_ENG] for e in bob_entries_post_T2),
        f"groups on bob: {[e['groups'] for e in bob_entries_post_T2]}",
    ))
    passed.append(harness.check(
        "after T2, carol STILL has 0 product entries (scope rule)",
        len(entries_tagged(CAROL["qdrant_port"], GROUP_PROD)) == 0,
    ))

    # ── T3: "Also share with product" ───────────────────────────────────
    print("\nT3: 'Also share with product' — add_groups + targeted push to product ONLY")
    add2 = mcp_alice.call_tool("add_groups", {
        "memory_ids": stored_ids, "groups": [GROUP_PROD],
    })
    passed.append(harness.check("add_groups: 4 updated for product",
                                len(add2.get("updated", [])) == 4, f"got {add2}"))

    push_prod = mcp_alice.call_tool("group_push", {
        "group": GROUP_PROD, "memory_ids": stored_ids,
    })
    carol_resp = next((p for p in push_prod.get("peers", [])
                       if p["node_name"] == CAROL["node_name"]), None)
    passed.append(harness.check("push to product: carol responsive",
                                carol_resp and carol_resp["status"] == "responsive",
                                f"got {carol_resp}"))
    passed.append(harness.check("push to product: 4 deliveries to carol",
                                carol_resp and len(carol_resp.get("deliveries", [])) == 4))
    passed.append(harness.check(
        "push to product: all 4 stored on carol",
        carol_resp and all(d.get("status") == "stored"
                          for d in carol_resp.get("deliveries", [])),
        f"deliveries: {carol_resp.get('deliveries') if carol_resp else None}",
    ))

    # SCOPE RULE: the product push must NOT contact bob.
    bob_in_prod_push = any(p["node_name"] == BOB["node_name"]
                           for p in push_prod.get("peers", []))
    passed.append(harness.check(
        "scope rule: bob (engineering-only peer) NOT contacted by product push",
        not bob_in_prod_push,
        "bob appeared in product push response — scope rule broken",
    ))

    # State after T3
    alice_groups_post_T3 = entry_groups_on(ALICE["qdrant_port"], stored_ids[0])
    passed.append(harness.check(
        "after T3, alice's memory tagged [personal, engineering, product]",
        set(alice_groups_post_T3) == {"personal", GROUP_ENG, GROUP_PROD},
        f"got {alice_groups_post_T3}",
    ))
    carol_entries_post_T3 = entries_tagged(CAROL["qdrant_port"], GROUP_PROD)
    passed.append(harness.check("after T3, carol has 4 product entries",
                                len(carol_entries_post_T3) == 4))
    passed.append(harness.check(
        "after T3, carol's entries tagged ONLY [product] (no personal or engineering leak)",
        all(e["groups"] == [GROUP_PROD] for e in carol_entries_post_T3),
        f"groups on carol: {[e['groups'] for e in carol_entries_post_T3]}",
    ))

    # CRITICAL: bob unchanged after the product push.
    bob_entries_post_T3 = entries_tagged(BOB["qdrant_port"], GROUP_ENG)
    passed.append(harness.check(
        "after T3, bob STILL has exactly 4 engineering entries (not re-contacted)",
        len(bob_entries_post_T3) == 4,
        f"got {len(bob_entries_post_T3)}",
    ))
    passed.append(harness.check(
        "after T3, bob's entries STILL tagged ONLY [engineering] (no product leak)",
        all(e["groups"] == [GROUP_ENG] for e in bob_entries_post_T3),
    ))

    # Cross-check: bob's search should find one of the shared memories
    print("\nVerify: bob's mem-fusion search finds Alice's gRPC decision")
    r = mcp_bob.call_tool("search_memory",
                          {"query": "what RPC protocol did we pick?", "top_k": 5})
    bob_finds_grpc = any("gRPC" in res["content"] for res in r.get("results", []))
    passed.append(harness.check("bob's search finds 'gRPC' content", bob_finds_grpc))

    # Cross-check: carol can also find it (product peer)
    r = mcp_carol.call_tool("search_memory",
                            {"query": "what RPC protocol did we pick?", "top_k": 5})
    carol_finds_grpc = any("gRPC" in res["content"] for res in r.get("results", []))
    passed.append(harness.check("carol's search finds 'gRPC' content", carol_finds_grpc))


def main():
    print("=" * 70)
    print("v0.4 store-now-share-later — 3-peer scope rule + push-time filter")
    print("=" * 70)

    if not all(p.exists() for p in (harness.QDRANT_BIN,
                                     harness.CONSTELLATION,
                                     harness.MEM_FUSION)):
        print("✗ missing dependency in harness", file=sys.stderr)
        sys.exit(1)

    procs = []
    with tempfile.TemporaryDirectory(prefix="mem-fusion-share-later-") as tdir:
        tmp = pathlib.Path(tdir)
        logs = tmp / "logs"; logs.mkdir()
        try:
            print("\nSTEP 1: start 3 Qdrants + init collections")
            for p in (ALICE, BOB, CAROL):
                d = tmp / p["node_name"]; d.mkdir()
                procs.append(harness.start_qdrant(
                    p["qdrant_port"], d, logs / f"qdrant-{p['node_name']}.log"))
                harness.init_cowork_memories(p["qdrant_port"])
                print(f"  ✓ Qdrant {p['node_name']} :{p['qdrant_port']}")

            print("\nSTEP 2: start 3 Constellation daemons + 3 mem-fusion subprocesses")
            # Alice is the origin — she knows bob (in engineering) and carol (in product).
            cfg_alice = tmp / "config-alice.json"
            harness.write_constellation_config(
                cfg_alice,
                node_name=ALICE["node_name"],
                peer_port=ALICE["peer_port"], gateway_port=ALICE["gateway_port"],
                qdrant_port=ALICE["qdrant_port"],
                state_dir=tmp / "state-alice",
                memberships=[
                    {"group_name": "personal", "peers": []},
                    {"group_name": GROUP_ENG, "peers": [
                        {"node_name": BOB["node_name"],
                         "endpoint": f"http://127.0.0.1:{BOB['peer_port']}"},
                    ]},
                    {"group_name": GROUP_PROD, "peers": [
                        {"node_name": CAROL["node_name"],
                         "endpoint": f"http://127.0.0.1:{CAROL['peer_port']}"},
                    ]},
                ],
            )
            # Bob is engineering-only — no product membership.
            cfg_bob = tmp / "config-bob.json"
            harness.write_constellation_config(
                cfg_bob,
                node_name=BOB["node_name"],
                peer_port=BOB["peer_port"], gateway_port=BOB["gateway_port"],
                qdrant_port=BOB["qdrant_port"],
                state_dir=tmp / "state-bob",
                memberships=[
                    {"group_name": "personal",  "peers": []},
                    {"group_name": GROUP_ENG,   "peers": []},
                ],
            )
            # Carol is product-only — no engineering membership.
            cfg_carol = tmp / "config-carol.json"
            harness.write_constellation_config(
                cfg_carol,
                node_name=CAROL["node_name"],
                peer_port=CAROL["peer_port"], gateway_port=CAROL["gateway_port"],
                qdrant_port=CAROL["qdrant_port"],
                state_dir=tmp / "state-carol",
                memberships=[
                    {"group_name": "personal",  "peers": []},
                    {"group_name": GROUP_PROD,  "peers": []},
                ],
            )

            for name, cfg, peer_port in [
                ("alice", cfg_alice, ALICE["peer_port"]),
                ("bob",   cfg_bob,   BOB["peer_port"]),
                ("carol", cfg_carol, CAROL["peer_port"]),
            ]:
                procs.append(harness.start_constellation(cfg, logs / f"const-{name}.log"))

            for name, port in [("alice", ALICE["peer_port"]),
                                ("bob",   BOB["peer_port"]),
                                ("carol", CAROL["peer_port"])]:
                if not harness.wait_health(port):
                    log_path = logs / f"const-{name}.log"
                    if log_path.exists():
                        print(f"--- {name} constellation log ---\n"
                              f"{log_path.read_text()[-2000:]}", file=sys.stderr)
                    raise RuntimeError(f"constellation {name} failed to come up")
            print("  ✓ 3 Constellation daemons up")

            # mem-fusion subprocesses — alice needs gateway URL (she initiates push);
            # bob and carol don't need to call group ops in this test but we wire
            # gateways anyway for parity.
            mcp_alice_proc = harness.start_mem_fusion(
                ALICE["qdrant_port"], logs / "mf-alice.stderr",
                gateway_url=f"http://127.0.0.1:{ALICE['gateway_port']}",
                node_name=ALICE["node_name"])
            mcp_bob_proc = harness.start_mem_fusion(
                BOB["qdrant_port"], logs / "mf-bob.stderr",
                gateway_url=f"http://127.0.0.1:{BOB['gateway_port']}",
                node_name=BOB["node_name"])
            mcp_carol_proc = harness.start_mem_fusion(
                CAROL["qdrant_port"], logs / "mf-carol.stderr",
                gateway_url=f"http://127.0.0.1:{CAROL['gateway_port']}",
                node_name=CAROL["node_name"])
            procs.extend([mcp_alice_proc, mcp_bob_proc, mcp_carol_proc])
            print("  ✓ 3 mem-fusion MCP subprocesses spawned")

            mcp_alice = harness.MCPClient(mcp_alice_proc)
            mcp_bob   = harness.MCPClient(mcp_bob_proc)
            mcp_carol = harness.MCPClient(mcp_carol_proc)

            passed = []
            run_tests(mcp_alice, mcp_bob, mcp_carol, passed)

            ok, total = sum(passed), len(passed)
            print(f"\n  {ok}/{total} invariants passed")
            if ok != total:
                print("\n✗ store-now-share-later FAILED", file=sys.stderr)
                for fn in ("mf-alice.stderr", "mf-bob.stderr", "mf-carol.stderr",
                           "const-alice.log", "const-bob.log", "const-carol.log"):
                    p = logs / fn
                    if p.exists():
                        print(f"--- {fn} (tail) ---\n{p.read_text()[-1500:]}",
                              file=sys.stderr)
                sys.exit(1)
            print("\n" + "=" * 70)
            print("✓ store-now-share-later PASSED")
            print("=" * 70)

        finally:
            for p in reversed(procs):
                harness.stop_proc(p)


if __name__ == "__main__":
    main()
