#!/usr/bin/env python3
"""
0.5.0-016 — /remember push <connector-id> orchestration primitives test.

Exercises the new MCP-exposed primitives that the SKILL.md procedural
section composes into a full push flow:

  - load_connectors_config (G3 schema validation)
  - build_connector_envelope (load_config + export_record + ABC compose)
  - get_connector_cursor_tool / set_connector_cursor_tool (cursor MCP)

NOT covered here: the Slack-side `slack_send_message` call. That happens
in Claude's reasoning inside the skill, against the Slack MCP layer
which mem-fusion has no access to (per architecture doc §6).

Pure-functional + Qdrant integration test. Runs against the LOCAL Qdrant.

Usage:
  ~/.local/share/mem-fusion/venv/bin/python tests/mem_fusion/test-connector-push-orchestration.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import uuid

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import core   # noqa: E402


def main():
    failures: list[str] = []
    cleanup_ids: list[str] = []
    cleanup_paths: list[str] = []

    def check(name: str, cond: bool, detail: str = ""):
        prefix = "  ✓" if cond else "  ✗"
        print(f"{prefix} {name}{(' — ' + detail) if detail else ''}")
        if not cond:
            failures.append(name)

    def tempjson(payload: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        cleanup_paths.append(path)
        return path

    # ───────────────────────────────────────────────────────────────────
    # load_connectors_config — G3 validation
    # ───────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("load_connectors_config (G3 schema validation)")
    print("=" * 60)

    cfg = core.load_connectors_config("/this/does/not/exist.json")
    check("missing file → error",
          bool(cfg["errors"]) and "not found" in cfg["errors"][0],
          cfg["errors"][0][:50] if cfg["errors"] else "")

    bad_json = tempfile.mkstemp(suffix=".json")[1]
    with open(bad_json, "w") as f:
        f.write("{not json")
    cleanup_paths.append(bad_json)
    cfg = core.load_connectors_config(bad_json)
    check("malformed JSON → error",
          any("not valid JSON" in e for e in cfg["errors"]))

    cfg = core.load_connectors_config(tempjson([1, 2, 3]))
    check("top-level non-object → error",
          any("must be a JSON object" in e for e in cfg["errors"]))

    cfg = core.load_connectors_config(tempjson({"connectors": "not a list"}))
    check("connectors not a list → error",
          any("'connectors' list" in e for e in cfg["errors"]))

    cfg = core.load_connectors_config(tempjson({"connectors": [
        {"id": "ok", "type": "slack", "channel": "C0AAA"},
        {"id": "ok", "type": "slack", "channel": "C0BBB"},
    ]}))
    check("duplicate id → error",
          any("duplicate id" in e for e in cfg["errors"]))

    cfg = core.load_connectors_config(tempjson({"connectors": [
        {"id": "x", "type": "discord", "channel": "anything"}
    ]}))
    check("unknown type → error",
          any("unknown type" in e for e in cfg["errors"]))

    cfg = core.load_connectors_config(tempjson({"connectors": [
        {"id": "x", "type": "slack"}  # no channel
    ]}))
    check("slack without channel → error",
          any("missing required field" in e for e in cfg["errors"]))

    cfg = core.load_connectors_config(tempjson({"connectors": [
        {"id": "engineering",      "type": "slack", "channel": "C0AAA"},
        {"id": "marketing",        "type": "slack", "channel": "C0BBB"},
        {"id": "personal-devices", "type": "slack", "channel": "C0CCC"},
    ]}))
    check("valid 3-entry config → no errors",
          not cfg["errors"] and len(cfg["connectors"]) == 3,
          str(cfg["errors"]) if cfg["errors"] else "")

    # ───────────────────────────────────────────────────────────────────
    # build_connector_envelope — full composition chain
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("build_connector_envelope (full composition chain)")
    print("=" * 60)

    async def composition_chain():
        cfg_path = tempjson({"connectors": [
            {"id": "test", "type": "slack", "channel": "C0B3VGB3RF1"},
        ]})
        core.CONNECTORS_CONFIG_PATH = pathlib.Path(cfg_path)

        # Real production-helper chain
        content = f"push-orch test {uuid.uuid4()}"
        sr = await core.store_memory({
            "content": content, "type": "context",
            "project": "test-016", "tags": ["push-orch-test"],
            "connector_ids": ["test"]})
        mid = sr["id"]
        cleanup_ids.append(mid)

        result = await core.build_connector_envelope({
            "memory_id": mid, "connector_id": "test"})

        check("returns body / channel / envelope / submitted_at / body_size",
              all(k in result for k in
                  ("body", "channel", "connector_id", "type", "submitted_at",
                   "envelope", "body_size")),
              f"keys={sorted(result.keys())}")
        check("channel comes from connector.json verbatim",
              result.get("channel") == "C0B3VGB3RF1")
        check("envelope.content equals source content",
              result.get("envelope", {}).get("content") == content)
        check("envelope.content_hash starts with 'sha256:' (wire form)",
              result.get("envelope", {}).get("content_hash", "").startswith("sha256:"))
        check("body starts with content (human-readable first)",
              result.get("body", "").startswith(content))
        check("body has fenced JSON envelope second",
              "```json" in result.get("body", ""))
        check("body under substrate cap (38000)",
              result.get("body_size", 0) < 38_000,
              f"size={result.get('body_size')}")

        # connector_not_found
        r = await core.build_connector_envelope({
            "memory_id": mid, "connector_id": "no-such-id"})
        check("unknown connector_id → connector_not_found",
              r.get("error") == "connector_not_found")

        # memory_not_found
        r = await core.build_connector_envelope({
            "memory_id": "00000000-0000-0000-0000-000000000000",
            "connector_id": "test"})
        check("unknown memory_id → memory_not_found",
              r.get("error") == "memory_not_found")

        # missing args
        r = await core.build_connector_envelope({"connector_id": "test"})
        check("missing memory_id → missing_argument",
              r.get("error") == "missing_argument")
        r = await core.build_connector_envelope({"memory_id": mid})
        check("missing connector_id → missing_argument",
              r.get("error") == "missing_argument")

    asyncio.run(composition_chain())

    # ───────────────────────────────────────────────────────────────────
    # Cursor MCP wrappers
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("get_connector_cursor_tool / set_connector_cursor_tool")
    print("=" * 60)

    async def cursor_round_trip():
        tmp = tempfile.mkstemp(suffix=".json")[1]
        os.unlink(tmp)  # ensure missing initially
        core.CONNECTOR_CURSORS_PATH = pathlib.Path(tmp)

        r = await core.get_connector_cursor_tool({"connector_id": "test"})
        check("missing cursor returns null", r.get("cursor") is None)

        r = await core.set_connector_cursor_tool({
            "connector_id": "test", "iso_ts": "2026-05-18T05:00:00Z"})
        check("set_connector_cursor → ok status",
              r.get("status") == "ok" and r.get("cursor") == "2026-05-18T05:00:00Z")

        r = await core.get_connector_cursor_tool({"connector_id": "test"})
        check("get_connector_cursor reads back the set value",
              r.get("cursor") == "2026-05-18T05:00:00Z")

        # Idempotent set
        r = await core.set_connector_cursor_tool({
            "connector_id": "test", "iso_ts": "2026-05-18T05:00:00Z"})
        check("idempotent set is a no-op observationally",
              r.get("status") == "ok")

        # Missing arg
        r = await core.get_connector_cursor_tool({})
        check("get with missing connector_id → missing_argument",
              r.get("error") == "missing_argument")
        r = await core.set_connector_cursor_tool({"connector_id": "test"})
        check("set with missing iso_ts → missing_argument",
              r.get("error") == "missing_argument")

        if os.path.exists(tmp):
            os.unlink(tmp)

    asyncio.run(cursor_round_trip())

    # ───────────────────────────────────────────────────────────────────
    # Cleanup
    # ───────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Cleanup")
    print("=" * 60)
    if cleanup_ids:
        core.qdrant.delete(collection_name=core.COLLECTION,
                           points_selector=cleanup_ids)
        print(f"  ✓ deleted {len(cleanup_ids)} test memories")
    for path in cleanup_paths:
        if os.path.exists(path):
            os.unlink(path)
    print(f"  ✓ removed {len(cleanup_paths)} temp config files")

    print()
    print("=" * 60)
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        sys.exit(1)
    print("ALL PUSH-ORCHESTRATION TESTS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
