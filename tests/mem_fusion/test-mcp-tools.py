#!/usr/bin/env python3
"""
mem-fusion MCP integration test.

Spins up an isolated Qdrant + launches mem_fusion.py as its MCP subprocess
over stdio JSON-RPC, then walks through all 9 local memory tools end-to-end
plus the graceful-degradation behavior of group_pull / group_push when no
Constellation gateway is running. No peer interactions.

Self-contained — uses tests/lib/harness.py for Qdrant + MCP lifecycle.

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/mem_fusion/test-mcp-tools.py
"""
import pathlib
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "lib"))
import core      # noqa: E402
import harness   # noqa: E402


QDRANT_PORT = 6733

# Tool list expected from mem_fusion.py — 10 local + 2 group = 12 total.
EXPECTED_TOOLS = {
    "store_memory", "search_memory", "search_recent", "upsert_memory",
    "find_or_create", "delete_memory", "get_related", "memory_stats",
    "export_record", "add_groups",
    "group_pull", "group_push",
}


def run_tests(mcp: harness.MCPClient) -> tuple[int, int]:
    passed = []

    print("\nSTEP 3: MCP initialize handshake")
    r = harness.mcp_initialize(mcp, client_name="mem-fusion-test")
    passed.append(harness.check("initialize returned a result", "result" in r))
    passed.append(harness.check("server identifies as mem-fusion",
                                r["result"]["serverInfo"]["name"] == "mem-fusion",
                                f"got {r['result'].get('serverInfo')}"))

    print("\nSTEP 4: tools/list returns all 12 tools (10 memory + 2 group)")
    r = mcp.request("tools/list", {})
    tool_names = {t["name"] for t in r["result"]["tools"]}
    passed.append(harness.check(
        f"all 12 expected tools present ({len(tool_names)} total)",
        EXPECTED_TOOLS.issubset(tool_names),
        f"missing: {EXPECTED_TOOLS - tool_names}",
    ))

    print("\nSTEP 5: memory_stats on empty collection")
    stats = mcp.call_tool("memory_stats", {})
    passed.append(harness.check("stats: total_memories == 0",
                                stats["total_memories"] == 0,
                                f"got {stats.get('total_memories')}"))
    passed.append(harness.check("stats: collection == cowork_memories",
                                stats["collection"] == "cowork_memories"))

    print("\nSTEP 6: store_memory — store first memory")
    paris = "The capital of France is Paris, the largest city in the country."
    r1 = mcp.call_tool("store_memory", {
        "content": paris, "type": "fact",
        "tags": ["geography"], "project": "mcp-test", "importance": 3,
    })
    passed.append(harness.check("store: status == 'stored'",
                                r1.get("status") == "stored", f"got {r1}"))
    paris_id = r1.get("id")
    passed.append(harness.check("store: returns an id", bool(paris_id)))

    print("\nSTEP 7: store_memory — store second, different memory")
    qm = "Quantum mechanics describes wave-particle duality at subatomic scales."
    r2 = mcp.call_tool("store_memory", {
        "content": qm, "type": "fact",
        "tags": ["physics"], "project": "mcp-test", "importance": 3,
    })
    qm_id = r2.get("id")
    passed.append(harness.check("store: second memory stored",
                                r2.get("status") == "stored"))

    print("\nSTEP 8: store_memory — duplicate content returns 'duplicate'")
    r3 = mcp.call_tool("store_memory", {"content": paris, "type": "fact"})
    passed.append(harness.check("duplicate detected",
                                r3.get("status") == "duplicate"))
    passed.append(harness.check("duplicate returns id matching original",
                                r3.get("id") == paris_id))
    passed.append(harness.check("default groups == [personal]",
                                r1.get("groups") == ["personal"], f"got {r1}"))

    print("\nSTEP 9: memory_stats reflects 2 stored")
    stats = mcp.call_tool("memory_stats", {})
    passed.append(harness.check("stats: total_memories == 2",
                                stats["total_memories"] == 2))
    passed.append(harness.check("stats: by_type['fact'] == 2",
                                stats["by_type"].get("fact") == 2))

    print("\nSTEP 10: search_memory — semantic query for 'France'")
    r = mcp.call_tool("search_memory",
                     {"query": "What is the capital of France?", "top_k": 5})
    passed.append(harness.check("search: at least 1 result", r["count"] >= 1))
    top = r["results"][0]
    passed.append(harness.check(f"search: top result is paris (score={top['score']})",
                                top["id"] == paris_id))
    passed.append(harness.check("search: top score > 0.75",
                                top["score"] > 0.75, f"score={top['score']}"))

    print("\nSTEP 11: search_memory with project filter")
    r = mcp.call_tool("search_memory",
                     {"query": "physics", "top_k": 5, "project": "mcp-test"})
    passed.append(harness.check("filtered search returns results", r["count"] >= 1))

    print("\nSTEP 12: export_record — full record with 768-dim vector")
    rec = mcp.call_tool("export_record", {"id": paris_id})
    passed.append(harness.check("export: id matches", rec["id"] == paris_id))
    passed.append(harness.check("export: content matches", rec["content"] == paris))
    passed.append(harness.check("export: vector len == 768",
                                isinstance(rec["vector"], list) and len(rec["vector"]) == 768))
    passed.append(harness.check("export: groups == [personal] (v0.4 default)",
                                rec.get("groups") == ["personal"], f"got {rec.get('groups')}"))
    passed.append(harness.check("export: origin_node set", bool(rec.get("origin_node"))))
    passed.append(harness.check("export: submitted_at set", bool(rec.get("submitted_at"))))

    print("\nSTEP 13: find_or_create — same content returns 'found'")
    r = mcp.call_tool("find_or_create",
                     {"content": paris, "type": "fact", "project": "mcp-test"})
    passed.append(harness.check("find_or_create: status == 'found'",
                                r.get("status") == "found", f"got {r}"))

    print("\nSTEP 14: find_or_create — new content creates")
    novel = "The mitochondrion is the powerhouse of the cell."
    r = mcp.call_tool("find_or_create",
                     {"content": novel, "type": "fact", "project": "mcp-test"})
    passed.append(harness.check("find_or_create: status == 'created'",
                                r.get("status") == "created", f"got {r}"))
    mito_id = r.get("id")

    print("\nSTEP 15: get_related — find memories similar to paris")
    r = mcp.call_tool("get_related", {"memory_id": paris_id, "top_k": 5})
    passed.append(harness.check("get_related: returns related list",
                                "results" in r and r["reference_id"] == paris_id))

    print("\nSTEP 16: upsert_memory — update content of paris")
    new_content = "Paris is the capital city of France and home to the Eiffel Tower."
    r = mcp.call_tool("upsert_memory",
                     {"id": paris_id, "content": new_content, "importance": 4})
    passed.append(harness.check("upsert: status == 'updated'",
                                r.get("status") == "updated"))
    rec = mcp.call_tool("export_record", {"id": paris_id})
    passed.append(harness.check("upsert: content was rewritten",
                                rec["content"] == new_content))
    passed.append(harness.check("upsert: content_hash changed",
                                rec["content_hash"] == core.content_hash(new_content)))

    print("\nSTEP 17: search_recent — recent memories")
    r = mcp.call_tool("search_recent", {"hours": 1, "top_k": 10})
    passed.append(harness.check(f"search_recent: 3 entries (got {r['count']})",
                                r["count"] == 3))

    print("\nSTEP 17b: add_groups — additively widens a memory's groups")
    r = mcp.call_tool("add_groups", {
        "memory_ids": [paris_id], "groups": ["engineering@test"],
    })
    passed.append(harness.check("add_groups: 1 updated, 0 no_op, 0 errors",
                                len(r.get("updated", [])) == 1
                                and not r.get("no_op")
                                and not r.get("errors"),
                                f"got {r}"))
    if r.get("updated"):
        passed.append(harness.check(
            "add_groups: union includes both personal and engineering@test",
            set(r["updated"][0]["groups"]) == {"personal", "engineering@test"},
            f"got {r['updated'][0]}"))

    print("\nSTEP 17c: add_groups again with same group → no_op")
    r = mcp.call_tool("add_groups", {
        "memory_ids": [paris_id], "groups": ["engineering@test"],
    })
    passed.append(harness.check("add_groups idempotent: no_op == 1",
                                len(r.get("no_op", [])) == 1, f"got {r}"))

    print("\nSTEP 18: group_pull — handles both gateway-present and gateway-absent gracefully")
    r = mcp.call_tool("group_pull", {})
    # Either: constellation_not_installed (fresh install, no daemon) OR a valid
    # peers-shaped response (gateway present and running). Both are correct
    # outcomes; the tool must NOT raise an uncaught exception in either case.
    ok = r.get("error") == "constellation_not_installed" or "peers" in r
    passed.append(harness.check(
        "group_pull degrades gracefully (either absent-error OR peers payload)",
        ok, f"got {r}",
    ))

    print("\nSTEP 19: group_push — handles both gateway-present and gateway-absent gracefully")
    r = mcp.call_tool("group_push", {"group": "engineering@test"})
    # Same rationale as STEP 18. With a gateway present, group_push to a
    # group this node isn't a member of returns a gateway_error (403);
    # without a gateway it returns constellation_not_installed. Both are
    # correct graceful outcomes.
    ok = (
        r.get("error") == "constellation_not_installed"
        or r.get("error") == "gateway_error"
        or "peers" in r
    )
    passed.append(harness.check(
        "group_push degrades gracefully (absent-error OR gateway-error OR peers payload)",
        ok, f"got {r}",
    ))

    print("\nSTEP 20: delete_memory — remove quantum-mechanics entry")
    r = mcp.call_tool("delete_memory", {"id": qm_id})
    passed.append(harness.check("delete: status == 'deleted'",
                                r.get("status") == "deleted"))

    print("\nSTEP 21: memory_stats reflects deletion")
    stats = mcp.call_tool("memory_stats", {})
    passed.append(harness.check(
        f"stats: total_memories == 2 after delete (got {stats['total_memories']})",
        stats["total_memories"] == 2,
    ))

    print("\nSTEP 22: delete_memory — remove mito entry")
    r = mcp.call_tool("delete_memory", {"id": mito_id})
    passed.append(harness.check("delete: status == 'deleted'",
                                r.get("status") == "deleted"))

    return sum(passed), len(passed)


def main():
    print("=" * 70)
    print("mem-fusion MCP integration — 11 tools via stdio JSON-RPC")
    print("=" * 70)

    if not harness.QDRANT_BIN.exists() or not harness.MEM_FUSION.exists():
        print(f"✗ missing dependency: qdrant={harness.QDRANT_BIN}, "
              f"mem_fusion={harness.MEM_FUSION}", file=sys.stderr)
        sys.exit(1)

    procs = []
    with tempfile.TemporaryDirectory(prefix="mem-fusion-mcp-test-") as tdir:
        tmp = pathlib.Path(tdir)
        try:
            print(f"\nSTEP 1: spin up Qdrant on :{QDRANT_PORT}")
            (tmp / "qdrant").mkdir()
            procs.append(harness.start_qdrant(QDRANT_PORT, tmp / "qdrant",
                                              tmp / "qdrant.log"))
            print(f"  ✓ Qdrant ready (log {tmp / 'qdrant.log'})")
            harness.init_cowork_memories(QDRANT_PORT)
            print(f"  ✓ collection '{harness.COLLECTION}' initialized")

            print("\nSTEP 2: spawn mem_fusion.py (no Constellation gateway env)")
            mcp_stderr = tmp / "mem-fusion.stderr"
            mcp_proc = harness.start_mem_fusion(QDRANT_PORT, mcp_stderr)
            procs.append(mcp_proc)
            print(f"  ✓ mem-fusion subprocess started (stderr {mcp_stderr})")

            mcp = harness.MCPClient(mcp_proc)
            ok, total = run_tests(mcp)
            print(f"\n  {ok}/{total} invariants passed")

            if ok != total:
                print("\n✗ MCP integration FAILED", file=sys.stderr)
                if mcp_stderr.exists():
                    print(f"--- mem-fusion stderr ---\n{mcp_stderr.read_text()}",
                          file=sys.stderr)
                sys.exit(1)

            print("\n" + "=" * 70)
            print("✓ MCP integration PASSED")
            print("=" * 70)

        finally:
            for p in reversed(procs):
                harness.stop_proc(p)


if __name__ == "__main__":
    main()
