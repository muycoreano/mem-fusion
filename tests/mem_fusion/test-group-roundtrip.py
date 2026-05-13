#!/usr/bin/env python3
"""
End-to-end group memory test — Claude's perspective.

Verifies the full chain works:
  Claude (test) → mem-fusion stdio MCP → Constellation gateway HTTP
                → remote peer's Constellation peer surface → remote Qdrant
                → remote mem-fusion's search_memory finds the content.

Six subprocesses in one test:
  - 2 Qdrants (each peer's local store)
  - 2 Constellation daemons (each peer-aware of the other)
  - 2 mem_fusion.py stdio MCP servers (env-pointed at the corresponding Qdrant
    + local Constellation gateway)

Two MCPClient instances drive the mem-fusions over JSON-RPC.

Self-contained — uses tests/lib/harness.py for all subprocess lifecycle.

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/mem_fusion/test-group-roundtrip.py
"""
import pathlib
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "lib"))
import core      # noqa: E402
import harness   # noqa: E402


GROUP_NAME = "test-roundtrip@e2e"

PEER_A = {
    "node_name":    "alice",
    "qdrant_port":  6843,
    "peer_port":    7843,
    "gateway_port": 7844,
}
PEER_B = {
    "node_name":    "bob",
    "qdrant_port":  6943,
    "peer_port":    7943,
    "gateway_port": 7944,
}


def run_tests(mcp_a: harness.MCPClient, mcp_b: harness.MCPClient,
              passed: list) -> None:
    # ── Init both MCP servers ───────────────────────────────────────────
    print("\nSTEP 3: MCP handshake on both mem-fusion subprocesses")
    harness.mcp_initialize(mcp_a, client_name="roundtrip-test-alice")
    harness.mcp_initialize(mcp_b, client_name="roundtrip-test-bob")
    passed.append(harness.check("both mem-fusion subprocesses initialized", True))

    # ── A → B: push direction ────────────────────────────────────────────
    print("\nSTEP 4: Alice stores a memory locally")
    paris = "We picked gRPC for internal RPC; HTTP/JSON for public APIs."
    r = mcp_a.call_tool("store_memory", {
        "content": paris, "type": "decision",
        "tags": ["architecture", "rpc"], "project": "roundtrip-test",
        "importance": 4,
    })
    passed.append(harness.check("alice store: status == 'stored'",
                                r.get("status") == "stored", f"got {r}"))
    paris_id = r.get("id")

    print("\nSTEP 5: Alice pushes to the group via mem-fusion/group_push")
    push = mcp_a.call_tool("group_push", {"id": paris_id})
    passed.append(harness.check("push response has peers list",
                                isinstance(push.get("peers"), list)))
    bob_entry = next((p for p in push.get("peers", [])
                      if p["node_name"] == PEER_B["node_name"]), None)
    passed.append(harness.check("push: bob responsive",
                                bob_entry and bob_entry["status"] == "responsive",
                                f"got {bob_entry}"))
    passed.append(harness.check("push: delivery == 'stored'",
                                bob_entry and bob_entry.get("delivery") == "stored"))

    print("\nSTEP 6: Bob's mem-fusion can find Alice's content via search_memory")
    r = mcp_b.call_tool("search_memory",
                       {"query": "what RPC protocol did we pick?", "top_k": 5})
    passed.append(harness.check("bob search: at least 1 result",
                                r["count"] >= 1, f"count={r['count']}"))
    if r["count"] >= 1:
        top = r["results"][0]
        passed.append(harness.check("bob search: top result contains 'gRPC'",
                                    "gRPC" in top["content"]))
        passed.append(harness.check(f"bob search: meaningful score (got {top['score']})",
                                    top["score"] > 0.5))

    # ── B → A: pull direction ────────────────────────────────────────────
    print("\nSTEP 7: Bob stores a different memory locally (no push)")
    tests_rule = "All new modules must include unit tests."
    r = mcp_b.call_tool("store_memory", {
        "content": tests_rule, "type": "preference",
        "tags": ["testing"], "project": "roundtrip-test",
        "importance": 4,
    })
    passed.append(harness.check("bob store: status == 'stored'",
                                r.get("status") == "stored"))
    tests_id = r.get("id")

    print("\nSTEP 8: Alice's search currently does NOT find Bob's memory (not yet shared)")
    r = mcp_a.call_tool("search_memory",
                       {"query": "what's our testing policy?", "top_k": 5})
    found_pre_share = any("unit tests" in res["content"] for res in r.get("results", []))
    passed.append(harness.check(
        "alice's search misses bob's un-pushed memory",
        not found_pre_share,
        "found it before push — privacy property broken",
    ))

    print("\nSTEP 9: Bob pushes the memory to the group")
    push2 = mcp_b.call_tool("group_push", {"id": tests_id})
    alice_entry = next((p for p in push2.get("peers", [])
                        if p["node_name"] == PEER_A["node_name"]), None)
    passed.append(harness.check("bob's push: alice responsive",
                                alice_entry and alice_entry["status"] == "responsive"))

    print("\nSTEP 10: Alice's search now finds Bob's memory")
    r = mcp_a.call_tool("search_memory",
                       {"query": "what's our testing policy?", "top_k": 5})
    found = any("unit tests" in res["content"] for res in r.get("results", []))
    passed.append(harness.check("alice's search finds bob's content after push", found))

    # ── Pull path: simulate offline-at-push-time, recover via pull ──────
    print("\nSTEP 11: Bob stores a third memory; we drop Alice's copy and pull instead")
    schemas = "JSON Schema is the canonical format for our API contracts."
    r = mcp_b.call_tool("store_memory", {
        "content": schemas, "type": "decision",
        "tags": ["api"], "project": "roundtrip-test",
        "importance": 3,
    })
    schemas_id = r.get("id")
    push3 = mcp_b.call_tool("group_push", {"id": schemas_id})  # arrives at A
    alice_entry = next((p for p in push3.get("peers", [])
                        if p["node_name"] == PEER_A["node_name"]), None)
    passed.append(harness.check("third push delivers to alice",
                                alice_entry and alice_entry["status"] == "responsive"))

    # Delete on Alice's side via direct Qdrant manipulation (simulate "lost")
    print("\n  → deleting the entry on alice's Qdrant to simulate it being lost")
    from qdrant_client import QdrantClient
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    client_a = QdrantClient(url=f"http://127.0.0.1:{PEER_A['qdrant_port']}", timeout=10)
    pts, _ = client_a.scroll(
        collection_name=harness.COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="content_hash",
                           match=MatchValue(value=core.content_hash(schemas))),
            FieldCondition(key="group_name",
                           match=MatchValue(value=GROUP_NAME)),
        ]),
        limit=10, with_payload=False,
    )
    for p in pts:
        client_a.delete(collection_name=harness.COLLECTION, points_selector=[p.id])
    print(f"  → deleted {len(pts)} entry from alice's local Qdrant")

    print("\nSTEP 12: Alice pulls; recovers Bob's memory via /memory/since")
    pull = mcp_a.call_tool("group_pull", {})
    bob_entry = next((p for p in pull.get("peers", [])
                      if p["node_name"] == PEER_B["node_name"]), None)
    passed.append(harness.check("alice's pull: bob responsive",
                                bob_entry and bob_entry["status"] == "responsive"))
    passed.append(harness.check(
        "alice's pull: at least one entry recovered",
        bob_entry and len(bob_entry.get("entry_ids", [])) >= 1,
        f"got {bob_entry}",
    ))

    print("\nSTEP 13: Alice's search finds the recovered memory")
    r = mcp_a.call_tool("search_memory",
                       {"query": "what's our API contract format?", "top_k": 5})
    found = any("JSON Schema" in res["content"] for res in r.get("results", []))
    passed.append(harness.check("alice's search finds the recovered content", found))


def main():
    print("=" * 70)
    print("Group memory round-trip — Claude → mem-fusion → Constellation → peer → search")
    print("=" * 70)

    if not all(p.exists() for p in (harness.QDRANT_BIN,
                                     harness.CONSTELLATION,
                                     harness.MEM_FUSION)):
        print("✗ missing dependency in harness", file=sys.stderr)
        sys.exit(1)

    procs = []
    with tempfile.TemporaryDirectory(prefix="mem-fusion-roundtrip-") as tdir:
        tmp = pathlib.Path(tdir)
        logs = tmp / "logs"; logs.mkdir()
        try:
            # ── STEP 1: start Qdrants + Constellations ──────────────────
            print("\nSTEP 1: start 2 Qdrants + 2 Constellation daemons")
            (tmp / "qa").mkdir(); (tmp / "qb").mkdir()
            procs.append(harness.start_qdrant(PEER_A["qdrant_port"], tmp / "qa",
                                              logs / "qdrant-a.log"))
            procs.append(harness.start_qdrant(PEER_B["qdrant_port"], tmp / "qb",
                                              logs / "qdrant-b.log"))
            harness.init_cowork_memories(PEER_A["qdrant_port"])
            harness.init_cowork_memories(PEER_B["qdrant_port"])
            print(f"  ✓ Qdrant alice :{PEER_A['qdrant_port']} + bob :{PEER_B['qdrant_port']}")

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
                        print(f"--- {fn} ---\n{p.read_text()[-2000:]}", file=sys.stderr)
                raise RuntimeError("Constellation daemons failed to come up")
            print(f"  ✓ Constellation alice (peer :{PEER_A['peer_port']}, "
                  f"gateway :{PEER_A['gateway_port']})")
            print(f"  ✓ Constellation bob   (peer :{PEER_B['peer_port']}, "
                  f"gateway :{PEER_B['gateway_port']})")

            # ── STEP 2: start mem-fusion subprocesses ───────────────────
            print("\nSTEP 2: spawn 2 mem-fusion MCP subprocesses")
            mcp_a_proc = harness.start_mem_fusion(
                PEER_A["qdrant_port"], logs / "mem-fusion-a.stderr",
                gateway_url=f"http://127.0.0.1:{PEER_A['gateway_port']}",
            )
            mcp_b_proc = harness.start_mem_fusion(
                PEER_B["qdrant_port"], logs / "mem-fusion-b.stderr",
                gateway_url=f"http://127.0.0.1:{PEER_B['gateway_port']}",
            )
            procs.append(mcp_a_proc)
            procs.append(mcp_b_proc)
            print(f"  ✓ mem-fusion alice (Qdrant :{PEER_A['qdrant_port']}, "
                  f"gateway :{PEER_A['gateway_port']})")
            print(f"  ✓ mem-fusion bob   (Qdrant :{PEER_B['qdrant_port']}, "
                  f"gateway :{PEER_B['gateway_port']})")

            mcp_a = harness.MCPClient(mcp_a_proc)
            mcp_b = harness.MCPClient(mcp_b_proc)

            passed = []
            run_tests(mcp_a, mcp_b, passed)

            ok, total = sum(passed), len(passed)
            print(f"\n  {ok}/{total} invariants passed")
            if ok != total:
                print("\n✗ Group round-trip FAILED", file=sys.stderr)
                for fn in ("mem-fusion-a.stderr", "mem-fusion-b.stderr",
                           "const-a.log", "const-b.log"):
                    p = logs / fn
                    if p.exists():
                        print(f"--- {fn} (tail) ---\n{p.read_text()[-1500:]}",
                              file=sys.stderr)
                sys.exit(1)
            print("\n" + "=" * 70)
            print("✓ Group round-trip PASSED")
            print("=" * 70)

        finally:
            for p in reversed(procs):
                harness.stop_proc(p)


if __name__ == "__main__":
    main()
