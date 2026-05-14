#!/usr/bin/env python3
"""
v0.4 offline / rejoin test — peer-symmetric catch-up via pull.

Tests the architectural property that any responsive peer in a group can
serve the full group history, including memories that originated on a peer
who is currently offline. This is what makes the "no orchestrator" design
resilient: data survives any single peer's downtime as long as one other
peer in the group has it.

Scenario (engineering group; alice/bob/carol are all members):

  T1.  Alice's stack starts. Bob and Carol are OFFLINE (no daemons).
  T2.  Alice stores 3 memories tagged groups=[engineering] (local-only;
       nobody to push to yet).
  T3.  Bob's stack starts.
  T4.  Bob calls group_pull(group=engineering). Alice → bob: 3 memories.
       Carol → bob: unreachable.
  T5.  Bob stores 2 NEW memories tagged groups=[engineering].
  T6.  Bob calls group_push(group=engineering). Alice receives 2;
       Carol unreachable.
  T7.  Bob's Constellation STOPS (bob goes offline).
  T8.  Carol's stack starts.
  T9.  Carol calls group_pull(group=engineering).
       Alice → carol: returns ALL 5 (alice's 3 originals + bob's 2 relayed).
       Bob → carol: unreachable.
  T10. Verify carol has all 5 memories with correct origin_node preserved.

Critical invariants:
  - Bob can pull from Alice without Carol being online.
  - Alice acts as a store-and-forward relay: Bob pushed memories TO her,
    and she serves them ON to Carol later via /memory/since.
  - Carol receives the full 5-memory set even though Bob is offline at her
    pull time — peer-symmetric resilience.
  - origin_node is preserved end-to-end (Carol's copies of Bob's memories
    still say origin_node=bob, not alice).

Three peers (all members of engineering):
  - alice  (origin):    memberships=[personal, engineering:[bob, carol]]
  - bob    (eng peer):  memberships=[personal, engineering:[alice, carol]]
  - carol  (eng peer):  memberships=[personal, engineering:[alice, bob]]

Each phase's start/stop is explicit so the offline scenario is faithful.

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/mem_fusion/test-offline-rejoin.py
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


GROUP_ENG = "engineering@offline-rejoin"

ALICE = {"node_name": "alice",
         "qdrant_port": 6543, "peer_port": 7543, "gateway_port": 7544}
BOB   = {"node_name": "bob",
         "qdrant_port": 6553, "peer_port": 7553, "gateway_port": 7554}
CAROL = {"node_name": "carol",
         "qdrant_port": 6563, "peer_port": 7563, "gateway_port": 7564}

ALICE_MEMORIES = [
    {"content": "v0.4 routes by group tag; classification is gone.",
     "type": "decision", "tags": ["v0.4"]},
    {"content": "Push-time filter strips groups recipient isn't in.",
     "type": "context", "tags": ["v0.4", "privacy"]},
    {"content": "Content_hash dedup is global, not per-group.",
     "type": "decision", "tags": ["v0.4", "dedup"]},
]
BOB_MEMORIES = [
    {"content": "add_groups widens an existing memory's group set additively.",
     "type": "decision", "tags": ["v0.4", "add_groups"]},
    {"content": "Each /memory/since call answers for exactly one group.",
     "type": "fact", "tags": ["v0.4", "pull"]},
]


def entries_tagged(qdrant_port: int, group: str) -> list[dict]:
    """Return entries on `qdrant_port` whose `groups` array contains `group`."""
    client = QdrantClient(url=f"http://127.0.0.1:{qdrant_port}", timeout=10)
    pts, _ = client.scroll(
        collection_name=core.COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="groups", match=MatchValue(value=group)),
        ]),
        limit=100, with_payload=True, with_vectors=False,
    )
    return [{
        "id":          str(p.id),
        "content":     (p.payload or {}).get("content", ""),
        "groups":      (p.payload or {}).get("groups", []),
        "origin_node": (p.payload or {}).get("origin_node", ""),
    } for p in pts]


def membership(group_name: str, peers: list[dict]) -> dict:
    return {"group_name": group_name, "peers": peers}


def write_config(path: pathlib.Path, peer: dict, peer_list: list[dict],
                 state_dir: pathlib.Path) -> None:
    harness.write_constellation_config(
        path,
        node_name=peer["node_name"],
        peer_port=peer["peer_port"], gateway_port=peer["gateway_port"],
        qdrant_port=peer["qdrant_port"],
        state_dir=state_dir,
        memberships=[
            membership("personal", []),
            membership(GROUP_ENG, peer_list),
        ],
    )


def start_stack(peer: dict, *, config_path: pathlib.Path,
                logs_dir: pathlib.Path, with_mcp: bool = True):
    """Spawn Constellation + (optionally) mem-fusion for one peer.
    Returns (constellation_proc, mcp_proc | None, MCPClient | None).
    Waits for Constellation peer-port health before returning."""
    const_proc = harness.start_constellation(
        config_path, logs_dir / f"const-{peer['node_name']}.log")
    if not harness.wait_health(peer["peer_port"]):
        log_path = logs_dir / f"const-{peer['node_name']}.log"
        if log_path.exists():
            print(f"--- {peer['node_name']} constellation log ---\n"
                  f"{log_path.read_text()[-2000:]}", file=sys.stderr)
        raise RuntimeError(f"constellation {peer['node_name']} failed to come up")

    if not with_mcp:
        return const_proc, None, None

    mcp_proc = harness.start_mem_fusion(
        peer["qdrant_port"],
        logs_dir / f"mf-{peer['node_name']}.stderr",
        gateway_url=f"http://127.0.0.1:{peer['gateway_port']}",
        node_name=peer["node_name"])
    mcp = harness.MCPClient(mcp_proc)
    harness.mcp_initialize(mcp, client_name=f"offline-rejoin-{peer['node_name']}")
    return const_proc, mcp_proc, mcp


def main():
    print("=" * 70)
    print("v0.4 offline / rejoin — peer-symmetric catch-up via pull")
    print("=" * 70)

    if not all(p.exists() for p in (harness.QDRANT_BIN,
                                     harness.CONSTELLATION,
                                     harness.MEM_FUSION)):
        print("✗ missing dependency in harness", file=sys.stderr)
        sys.exit(1)

    passed: list[bool] = []
    procs: list = []

    with tempfile.TemporaryDirectory(prefix="mem-fusion-offline-") as tdir:
        tmp = pathlib.Path(tdir)
        logs = tmp / "logs"; logs.mkdir()
        try:
            # ── Bring up all three Qdrants up-front; cheap to keep running ──
            print("\nSetup: start 3 Qdrants (cheap; daemons gate the offline scenario)")
            for peer in (ALICE, BOB, CAROL):
                d = tmp / peer["node_name"]; d.mkdir()
                procs.append(harness.start_qdrant(
                    peer["qdrant_port"], d,
                    logs / f"qdrant-{peer['node_name']}.log"))
                harness.init_cowork_memories(peer["qdrant_port"])
                print(f"  ✓ Qdrant {peer['node_name']} :{peer['qdrant_port']}")

            # Write each peer's config now; daemons start in phases below.
            cfg_alice = tmp / "config-alice.json"
            cfg_bob   = tmp / "config-bob.json"
            cfg_carol = tmp / "config-carol.json"
            write_config(cfg_alice, ALICE, peer_list=[
                {"node_name": BOB["node_name"],
                 "endpoint": f"http://127.0.0.1:{BOB['peer_port']}"},
                {"node_name": CAROL["node_name"],
                 "endpoint": f"http://127.0.0.1:{CAROL['peer_port']}"},
            ], state_dir=tmp / "state-alice")
            write_config(cfg_bob, BOB, peer_list=[
                {"node_name": ALICE["node_name"],
                 "endpoint": f"http://127.0.0.1:{ALICE['peer_port']}"},
                {"node_name": CAROL["node_name"],
                 "endpoint": f"http://127.0.0.1:{CAROL['peer_port']}"},
            ], state_dir=tmp / "state-bob")
            write_config(cfg_carol, CAROL, peer_list=[
                {"node_name": ALICE["node_name"],
                 "endpoint": f"http://127.0.0.1:{ALICE['peer_port']}"},
                {"node_name": BOB["node_name"],
                 "endpoint": f"http://127.0.0.1:{BOB['peer_port']}"},
            ], state_dir=tmp / "state-carol")

            # ── T1: Alice starts. Bob and Carol are OFFLINE. ───────────────
            print("\nT1: Alice's stack starts; bob & carol have NO Constellation daemons")
            alice_const, alice_mcp_proc, mcp_alice = start_stack(
                ALICE, config_path=cfg_alice, logs_dir=logs)
            procs.extend([alice_const, alice_mcp_proc])
            print("  ✓ alice up; bob, carol offline")

            # ── T2: Alice stores 3 memories tagged [engineering] ───────────
            print("\nT2: Alice stores 3 memories tagged [engineering] (local-only — peers offline)")
            alice_ids: list[str] = []
            for mem in ALICE_MEMORIES:
                r = mcp_alice.call_tool("store_memory", {
                    **mem, "project": "offline-rejoin-test", "importance": 4,
                    "groups": [GROUP_ENG],
                })
                if r.get("status") != "stored":
                    print(f"  ✗ alice store failed: {r}", file=sys.stderr); sys.exit(1)
                alice_ids.append(r["id"])
            passed.append(harness.check("alice stored 3 engineering memories",
                                        len(entries_tagged(ALICE["qdrant_port"], GROUP_ENG)) == 3))

            # ── T3: Bob's stack starts ─────────────────────────────────────
            print("\nT3: Bob's stack starts (carol still offline)")
            bob_const, bob_mcp_proc, mcp_bob = start_stack(
                BOB, config_path=cfg_bob, logs_dir=logs)
            procs.extend([bob_const, bob_mcp_proc])
            print("  ✓ bob up")

            # ── T4: Bob pulls; alice responsive, carol unreachable ─────────
            print("\nT4: Bob pulls engineering — alice responsive, carol unreachable")
            pull = mcp_bob.call_tool("group_pull", {"group": GROUP_ENG})
            by_peer = {p["node_name"]: p for p in pull.get("peers", [])}
            passed.append(harness.check(
                "bob's pull: alice responsive",
                by_peer.get(ALICE["node_name"], {}).get("status") == "responsive",
                f"alice entry: {by_peer.get(ALICE['node_name'])}"))
            passed.append(harness.check(
                "bob's pull: 3 entries stored from alice",
                len(by_peer.get(ALICE["node_name"], {}).get("entry_ids", [])) == 3))
            passed.append(harness.check(
                "bob's pull: carol unreachable",
                by_peer.get(CAROL["node_name"], {}).get("status") == "unreachable"))
            bob_after_pull = entries_tagged(BOB["qdrant_port"], GROUP_ENG)
            passed.append(harness.check(
                "bob now has 3 engineering memories locally",
                len(bob_after_pull) == 3, f"got {len(bob_after_pull)}"))
            passed.append(harness.check(
                "bob's copies preserve origin_node=alice",
                all(e["origin_node"] == ALICE["node_name"] for e in bob_after_pull),
                f"origins: {[e['origin_node'] for e in bob_after_pull]}"))

            # ── T5: Bob stores 2 new memories ──────────────────────────────
            print("\nT5: Bob stores 2 NEW memories tagged [engineering]")
            bob_ids: list[str] = []
            for mem in BOB_MEMORIES:
                r = mcp_bob.call_tool("store_memory", {
                    **mem, "project": "offline-rejoin-test", "importance": 4,
                    "groups": [GROUP_ENG],
                })
                if r.get("status") != "stored":
                    print(f"  ✗ bob store failed: {r}", file=sys.stderr); sys.exit(1)
                bob_ids.append(r["id"])
            passed.append(harness.check("bob has 5 engineering memories (3 pulled + 2 own)",
                                        len(entries_tagged(BOB["qdrant_port"], GROUP_ENG)) == 5))

            # ── T6: Bob pushes; alice receives, carol unreachable ──────────
            print("\nT6: Bob pushes engineering — alice receives, carol unreachable")
            push = mcp_bob.call_tool("group_push", {
                "group": GROUP_ENG, "memory_ids": bob_ids,
            })
            by_peer = {p["node_name"]: p for p in push.get("peers", [])}
            alice_entry = by_peer.get(ALICE["node_name"])
            passed.append(harness.check(
                "bob's push: alice responsive",
                alice_entry and alice_entry["status"] == "responsive"))
            passed.append(harness.check(
                "bob's push: 2 deliveries to alice all stored",
                alice_entry
                and len(alice_entry.get("deliveries", [])) == 2
                and all(d.get("status") == "stored"
                        for d in alice_entry.get("deliveries", []))))
            passed.append(harness.check(
                "bob's push: carol unreachable",
                by_peer.get(CAROL["node_name"], {}).get("status") == "unreachable"))

            alice_post = entries_tagged(ALICE["qdrant_port"], GROUP_ENG)
            passed.append(harness.check(
                "alice now has 5 engineering memories (her 3 + bob's 2)",
                len(alice_post) == 5))
            alice_origins = {e["origin_node"] for e in alice_post}
            passed.append(harness.check(
                "alice's set spans both origin_nodes (alice + bob)",
                alice_origins == {ALICE["node_name"], BOB["node_name"]},
                f"got origins: {sorted(alice_origins)}"))

            # ── T7: Bob's Constellation STOPS (bob goes offline) ──────────
            print("\nT7: Bob's Constellation daemon stops — bob goes OFFLINE")
            harness.stop_proc(bob_const)
            # Remove from procs so cleanup at end doesn't re-stop a dead one.
            procs.remove(bob_const)
            print("  ✓ bob's constellation stopped")

            # ── T8: Carol's stack starts ──────────────────────────────────
            print("\nT8: Carol's stack starts (bob is offline)")
            carol_const, carol_mcp_proc, mcp_carol = start_stack(
                CAROL, config_path=cfg_carol, logs_dir=logs)
            procs.extend([carol_const, carol_mcp_proc])
            print("  ✓ carol up; bob still offline")

            # ── T9: Carol pulls — should receive ALL 5 via Alice ──────────
            print("\nT9: Carol pulls engineering — alice responsive, bob unreachable")
            pull_c = mcp_carol.call_tool("group_pull", {"group": GROUP_ENG})
            by_peer = {p["node_name"]: p for p in pull_c.get("peers", [])}
            alice_entry = by_peer.get(ALICE["node_name"])
            bob_entry   = by_peer.get(BOB["node_name"])
            passed.append(harness.check(
                "carol's pull: alice responsive",
                alice_entry and alice_entry["status"] == "responsive"))
            passed.append(harness.check(
                "carol's pull: 5 entries stored from alice (alice's 3 + bob's 2 relayed)",
                len(alice_entry.get("entry_ids", [])) == 5
                if alice_entry else False,
                f"alice entry: {alice_entry}"))
            passed.append(harness.check(
                "carol's pull: bob unreachable",
                bob_entry and bob_entry["status"] == "unreachable",
                f"bob entry: {bob_entry}"))

            # ── T10: verify carol's local store has all 5 with correct origins ─
            print("\nT10: Verify carol has all 5 memories with correct origin_nodes")
            carol_post = entries_tagged(CAROL["qdrant_port"], GROUP_ENG)
            passed.append(harness.check(
                "carol has 5 engineering memories",
                len(carol_post) == 5, f"got {len(carol_post)}"))

            carol_origins = {}
            for e in carol_post:
                carol_origins.setdefault(e["origin_node"], 0)
                carol_origins[e["origin_node"]] += 1
            passed.append(harness.check(
                "carol's 5 memories: 3 from alice + 2 from bob (origin_node preserved)",
                carol_origins == {ALICE["node_name"]: 3, BOB["node_name"]: 2},
                f"got {carol_origins}"))

            # Carol's mem-fusion search should find content from both alice and bob
            print("\nT10b: Carol's search finds Bob-originated content via Alice's relay")
            r = mcp_carol.call_tool("search_memory",
                                    {"query": "add_groups widens a memory's group set",
                                     "top_k": 5})
            found_bob = any("add_groups" in res["content"] for res in r.get("results", []))
            passed.append(harness.check(
                "carol's search finds bob's add_groups memory (despite bob offline)",
                found_bob, "search missed it — peer-symmetric resilience broken"))

            r = mcp_carol.call_tool("search_memory",
                                    {"query": "push-time filter strips groups",
                                     "top_k": 5})
            found_alice = any("push-time" in res["content"].lower()
                              for res in r.get("results", []))
            passed.append(harness.check(
                "carol's search finds alice's push-time-filter memory",
                found_alice))

            ok, total = sum(passed), len(passed)
            print(f"\n  {ok}/{total} invariants passed")
            if ok != total:
                print("\n✗ offline-rejoin FAILED", file=sys.stderr)
                for fn in ("mf-alice.stderr", "mf-bob.stderr", "mf-carol.stderr",
                           "const-alice.log", "const-bob.log", "const-carol.log"):
                    p = logs / fn
                    if p.exists():
                        print(f"--- {fn} (tail) ---\n{p.read_text()[-1500:]}",
                              file=sys.stderr)
                sys.exit(1)
            print("\n" + "=" * 70)
            print("✓ offline-rejoin PASSED — peer-symmetric resilience holds")
            print("=" * 70)

        finally:
            for p in reversed(procs):
                harness.stop_proc(p)


if __name__ == "__main__":
    main()
