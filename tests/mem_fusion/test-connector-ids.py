#!/usr/bin/env python3
"""
E-A / E-B / E-C tests — v0.5 connector_ids field, cursor persistence,
add_connector_ids MCP tool (architecture doc §4 + memory 0daf2f0c).

Runs against the LOCAL running Qdrant. Uses a unique project tag
(`test-connector-ids-<timestamp>`) for isolation; cleans up all
test-created memories before exit, leaving production memories untouched.
Also temp-redirects the cursor file via MEMFUSION_CONNECTOR_CURSORS env
var so it doesn't touch the user's real connector_cursors.json.

Exit code: 0 on all-pass, 1 on any failure.

Usage:
  ~/.local/share/mem-fusion/venv/bin/python tests/mem_fusion/test-connector-ids.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import time
import uuid


def main():
    # Test-isolated cursor file via env var BEFORE importing core
    tmp_cursor = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    )
    tmp_cursor.close()
    pathlib.Path(tmp_cursor.name).unlink()  # remove so "missing file" cases work
    os.environ["MEMFUSION_CONNECTOR_CURSORS"] = tmp_cursor.name

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root / "src"))
    import core  # noqa: E402

    project_tag = f"test-connector-ids-{int(time.time())}"
    created_ids: list[str] = []
    failures: list[str] = []

    def check(name: str, cond: bool, msg: str = ""):
        prefix = "  ✓" if cond else "  ✗"
        print(f"{prefix} {name}{(' — ' + msg) if msg else ''}")
        if not cond:
            failures.append(name)

    print("=" * 60)
    print("E-A: connector_ids field round-trip + filter + back-compat")
    print("=" * 60)

    # ── 1. store with connector_ids=["foo"] → persisted ────────────────
    r1 = asyncio.run(core.store_memory({
        "content": f"E-A test 1: connector_ids basic round-trip {uuid.uuid4()}",
        "type": "context", "project": project_tag,
        "connector_ids": ["test-connector-foo"],
    }))
    check("store with connector_ids persists",
          r1.get("status") == "stored" and r1.get("connector_ids") == ["test-connector-foo"],
          f"got: {r1}")
    if "id" in r1:
        created_ids.append(r1["id"])

    # ── 2. store WITHOUT connector_ids → defaults to [] ────────────────
    r2 = asyncio.run(core.store_memory({
        "content": f"E-A test 2: default empty connector_ids {uuid.uuid4()}",
        "type": "context", "project": project_tag,
    }))
    check("store without connector_ids defaults to []",
          r2.get("status") == "stored" and r2.get("connector_ids") == [],
          f"got: {r2}")
    if "id" in r2:
        created_ids.append(r2["id"])

    # ── 3. dedup-merge widens connector_ids when same content seen with new id ─
    same_content = f"E-A test 3: dedup-merge with widened connectors {uuid.uuid4()}"
    r3a = asyncio.run(core.store_memory({
        "content": same_content, "type": "context", "project": project_tag,
        "connector_ids": ["test-connector-foo"],
    }))
    check("first store seeds connector_ids=['test-connector-foo']",
          r3a.get("status") == "stored" and r3a.get("connector_ids") == ["test-connector-foo"])
    if "id" in r3a:
        created_ids.append(r3a["id"])

    r3b = asyncio.run(core.store_memory({
        "content": same_content, "type": "context", "project": project_tag,
        "connector_ids": ["test-connector-bar"],
    }))
    check("re-store with new connector_id triggers merged status",
          r3b.get("status") == "merged" and
          set(r3b.get("connector_ids", [])) == {"test-connector-foo", "test-connector-bar"},
          f"got: {r3b}")

    r3c = asyncio.run(core.store_memory({
        "content": same_content, "type": "context", "project": project_tag,
        "connector_ids": ["test-connector-foo"],
    }))
    check("re-store with already-present connector_id is duplicate (no widening)",
          r3c.get("status") == "duplicate" and
          set(r3c.get("connector_ids", [])) == {"test-connector-foo", "test-connector-bar"},
          f"got: {r3c}")

    # ── 4. search_memory(connector_id=...) filters correctly ───────────
    sr = asyncio.run(core.search_memory({
        "query": f"connector test {project_tag}",
        "top_k": 20, "connector_id": "test-connector-foo", "project": project_tag,
    }))
    matches = sr.get("results", [])
    all_have_foo = all("test-connector-foo" in r.get("connector_ids", []) for r in matches)
    check("search_memory(connector_id) returns only memories tagged with that id",
          len(matches) >= 1 and all_have_foo,
          f"got {len(matches)} hits, all match: {all_have_foo}")

    # ── 5. export_record includes connector_ids ────────────────────────
    if r1.get("id"):
        er = asyncio.run(core.export_record({"id": r1["id"]}))
        check("export_record returns connector_ids field",
              er.get("connector_ids") == ["test-connector-foo"],
              f"got: {er.get('connector_ids')}")

    # ── 6. v0.4 back-compat — manually-injected payload without connector_ids field ─
    # Simulate a v0.4 memory by directly setting payload without connector_ids
    from qdrant_client.models import PointStruct
    legacy_id = str(uuid.uuid4())
    legacy_vec = asyncio.run(core.embed("legacy v0.4 memory simulation"))
    if isinstance(legacy_vec, list):
        core.qdrant.upsert(collection_name=core.COLLECTION, points=[PointStruct(
            id=legacy_id, vector=legacy_vec,
            payload={
                "content": "legacy v0.4 memory simulation", "type": "context",
                "project": project_tag, "importance": 3,
                "content_hash": core.content_hash("legacy v0.4 memory simulation"),
                "timestamp": core.iso_now(), "groups": ["personal"],
                "origin_node": "test-peer", "submitted_at": core.iso_now(),
                # NOTE: no connector_ids field — this is what v0.4 entries look like
            },
        )])
        created_ids.append(legacy_id)
        er_legacy = asyncio.run(core.export_record({"id": legacy_id}))
        check("legacy v0.4 entry (no connector_ids in payload) deserializes with []",
              er_legacy.get("connector_ids") == [],
              f"got: {er_legacy.get('connector_ids')}")

    print()
    print("=" * 60)
    print("E-B: per-connector cursor persistence")
    print("=" * 60)

    # ── 7. get on missing file returns None ────────────────────────────
    check("get_connector_cursor on missing file returns None",
          core.get_connector_cursor("nonexistent") is None)

    # ── 8. set + get round-trip ────────────────────────────────────────
    core.set_connector_cursor("test-conn-1", "2026-05-17T12:00:00+00:00")
    val = core.get_connector_cursor("test-conn-1")
    check("set/get round-trip works",
          val == "2026-05-17T12:00:00+00:00", f"got: {val}")

    # ── 9. get on absent key returns None even with file present ───────
    check("get_connector_cursor on absent key returns None (file exists)",
          core.get_connector_cursor("test-conn-absent") is None)

    # ── 10. multi-key writes don't clobber ─────────────────────────────
    core.set_connector_cursor("test-conn-2", "2026-05-17T13:00:00+00:00")
    check("set on second key preserves first",
          core.get_connector_cursor("test-conn-1") == "2026-05-17T12:00:00+00:00" and
          core.get_connector_cursor("test-conn-2") == "2026-05-17T13:00:00+00:00")

    # ── 11. corrupt JSON gracefully returns None ───────────────────────
    cursor_path = pathlib.Path(os.environ["MEMFUSION_CONNECTOR_CURSORS"])
    backup = cursor_path.read_text()
    cursor_path.write_text("{ this is not valid json")
    check("corrupt JSON file returns None (no crash)",
          core.get_connector_cursor("test-conn-1") is None)
    cursor_path.write_text(backup)  # restore

    # ── 12. empty connector_id arg returns None (no exception) ─────────
    check("get_connector_cursor('') returns None safely",
          core.get_connector_cursor("") is None)

    print()
    print("=" * 60)
    print("E-C: add_connector_ids MCP tool")
    print("=" * 60)

    # Create a fresh memory to mutate
    seed = asyncio.run(core.store_memory({
        "content": f"E-C test seed: add_connector_ids on this memory {uuid.uuid4()}",
        "type": "context", "project": project_tag,
    }))
    seed_id = seed.get("id")
    created_ids.append(seed_id)

    # ── 13. add to memory without connector_ids → updated, set to [foo] ─
    r13 = asyncio.run(core.add_connector_ids({
        "memory_ids": [seed_id], "connector_ids": ["test-c-x"],
    }))
    updated = r13.get("updated", [])
    check("add to memory without connector_ids → updated",
          len(updated) == 1 and updated[0]["connector_ids"] == ["test-c-x"] and updated[0]["added"] == ["test-c-x"],
          f"got: {r13}")

    # ── 14. add same again → no_op ─────────────────────────────────────
    r14 = asyncio.run(core.add_connector_ids({
        "memory_ids": [seed_id], "connector_ids": ["test-c-x"],
    }))
    no_op = r14.get("no_op", [])
    check("add same connector_id → no_op",
          len(no_op) == 1 and no_op[0]["connector_ids"] == ["test-c-x"],
          f"got: {r14}")

    # ── 15. add different → union ─────────────────────────────────────
    r15 = asyncio.run(core.add_connector_ids({
        "memory_ids": [seed_id], "connector_ids": ["test-c-y", "test-c-x"],
    }))
    updated15 = r15.get("updated", [])
    check("add union with existing → updated with both",
          len(updated15) == 1 and
          set(updated15[0]["connector_ids"]) == {"test-c-x", "test-c-y"} and
          updated15[0]["added"] == ["test-c-y"],
          f"got: {r15}")

    # ── 16. add to non-existent id → error ─────────────────────────────
    r16 = asyncio.run(core.add_connector_ids({
        "memory_ids": ["00000000-0000-0000-0000-000000000000"], "connector_ids": ["test-c-z"],
    }))
    errs = r16.get("errors", [])
    check("add to non-existent id → error",
          len(errs) == 1 and errs[0]["reason"] == "not_found",
          f"got: {r16}")

    # ── 17. empty connector_ids → missing_argument ─────────────────────
    r17 = asyncio.run(core.add_connector_ids({
        "memory_ids": [seed_id], "connector_ids": [],
    }))
    check("empty connector_ids → missing_argument error",
          r17.get("error") == "missing_argument", f"got: {r17}")

    # ── 18. empty memory_ids → missing_argument ────────────────────────
    r18 = asyncio.run(core.add_connector_ids({
        "memory_ids": [], "connector_ids": ["test-c-x"],
    }))
    check("empty memory_ids → missing_argument error",
          r18.get("error") == "missing_argument", f"got: {r18}")

    # ───────────────────────────────────────────────────────────────────
    # Cleanup — remove all test-created memories so production store stays clean
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Cleanup")
    print("=" * 60)
    if created_ids:
        core.qdrant.delete(collection_name=core.COLLECTION,
                           points_selector=[i for i in created_ids if i])
        print(f"  ✓ deleted {len(created_ids)} test memories")
    pathlib.Path(os.environ["MEMFUSION_CONNECTOR_CURSORS"]).unlink(missing_ok=True)
    print(f"  ✓ removed temp cursor file {os.environ['MEMFUSION_CONNECTOR_CURSORS']}")

    print()
    print("=" * 60)
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        sys.exit(1)
    print("ALL TESTS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
