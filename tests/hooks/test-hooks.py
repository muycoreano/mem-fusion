#!/usr/bin/env python3
"""
Hook script tests — compile-check + end-to-end smoke for all 4 Claude Code hooks.

The hooks ship as part of the mem-fusion install and fire in response to
Claude Code session events. They hardcode `$HOME/.local/share/mem-fusion/`
paths, so the test stages a fake HOME with the mem-fusion-shaped directory
layout (core.py copied in, venv symlinked, scripts copied in) and runs each
hook with HOME overridden to point there.

This catches the class of bug WP6 missed: hooks that import legacy function
names that don't exist on the production module surface. The test exercises
the real shell → Python → core.* chain, so an `AttributeError` like
`mem_fusion has no attribute 'tool_search'` would surface as a non-zero exit
or empty / error output from the hook.

Four hooks tested:
  - session_prime.sh         (SessionStart)
  - prompt_memory_inject.sh  (UserPromptSubmit)
  - capture_file_write.sh    (PostToolUse:Write)
  - ingest_session.py        (Stop)

Two passes per hook:
  - Compile: bash -n / python -m py_compile — fast, no infrastructure
  - Smoke run: subprocess invocation against a fresh Qdrant in tempdir HOME

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/hooks/test-hooks.py
"""
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "lib"))
import core      # noqa: E402
import harness   # noqa: E402

from qdrant_client import QdrantClient

QDRANT_PORT = 6753

HOOK_SOURCES = [
    REPO_ROOT / "src/scripts/session_prime.sh",
    REPO_ROOT / "src/scripts/prompt_memory_inject.sh",
    REPO_ROOT / "src/scripts/capture_file_write.sh",
    REPO_ROOT / "src/scripts/ingest_session.py",
]


def run_hook(script_path, *, fake_home, stdin: str = "",
             env_extra: dict | None = None, timeout_s: float = 30.0):
    """Wrapper around harness.run_hook that fills in our Qdrant port."""
    return harness.run_hook(script_path, fake_home=fake_home,
                            qdrant_port=QDRANT_PORT, stdin=stdin,
                            env_extra=env_extra, timeout_s=timeout_s)


def write_fake_session(home: pathlib.Path, session_id: str) -> pathlib.Path:
    """Write a fake .jsonl session that clears the quality gate (≥4 human turns).

    The ingest_session.py extractor looks for preference / decision / error
    patterns; we plant one of each across the messages.
    """
    sessions = home / ".claude/sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"{session_id}.jsonl"
    lines = [
        {"role": "human",
         "content": "I prefer to always use uv for Python environments going forward."},
        {"role": "assistant",
         "content": "Got it. I'll always use uv from now on."},
        {"role": "human",
         "content": "Decision time on the auth service database."},
        {"role": "assistant",
         "content": "We decided to use PostgreSQL 16 with logical replication for the auth service."},
        {"role": "human",
         "content": "There's a JWT clock-skew bug breaking auth in staging."},
        {"role": "assistant",
         "content": "The fix was to add 30 seconds of leeway in the JWT validation chain."},
        {"role": "human",
         "content": "Sounds good — also remember we are going with PostgreSQL 16 for this project."},
        {"role": "assistant",
         "content": "Noted. The decision is locked in."},
    ]
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return path


def populate_seed_memories(qdrant_url: str) -> list[str]:
    """Embed and store 3 seed memories so search-based hooks have hits.

    Uses core.store_memory against the test Qdrant (which means real Ollama
    embeddings — same dependency the other test suites carry).
    """
    core.QDRANT_URL = qdrant_url
    core.qdrant = QdrantClient(url=qdrant_url, timeout=10)
    seeds = [
        {"content": "We picked gRPC for internal RPC; HTTP/JSON for public APIs.",
         "type": "decision", "tags": ["rpc"], "project": "hook-test", "importance": 4},
        {"content": "PostgreSQL 16 with logical replication is the auth-service store.",
         "type": "decision", "tags": ["database"], "project": "hook-test", "importance": 4},
        {"content": "I prefer to use uv for managing Python virtual environments.",
         "type": "preference", "tags": ["python"], "project": "hook-test", "importance": 4},
    ]
    ids = []
    for s in seeds:
        r = asyncio.run(core.store_memory(s))
        if r.get("status") != "stored":
            raise RuntimeError(f"seed store failed: {r}")
        ids.append(r["id"])
    return ids


def count_memories(qdrant_url: str) -> int:
    client = QdrantClient(url=qdrant_url, timeout=10)
    info = client.get_collection(core.COLLECTION)
    return info.points_count or 0


def main() -> None:
    print("=" * 70)
    print("Hook script tests — compile + end-to-end smoke for all 4 hooks")
    print("=" * 70)

    if not harness.QDRANT_BIN.exists():
        print(f"✗ missing qdrant binary at {harness.QDRANT_BIN}", file=sys.stderr)
        sys.exit(1)

    passed: list[bool] = []
    procs: list = []
    qdrant_url = f"http://127.0.0.1:{QDRANT_PORT}"

    with tempfile.TemporaryDirectory(prefix="mem-fusion-hooks-") as tdir:
        tmp = pathlib.Path(tdir)
        logs = tmp / "logs"; logs.mkdir()
        try:
            # ── STEP 1 — compile-check all 4 hooks ─────────────────────
            print("\nSTEP 1: compile-check each hook (bash -n / python -m py_compile)")
            for src in HOOK_SOURCES:
                if src.suffix == ".sh":
                    p = subprocess.run(["bash", "-n", str(src)],
                                       capture_output=True, text=True)
                else:
                    p = subprocess.run([sys.executable, "-m", "py_compile", str(src)],
                                       capture_output=True, text=True)
                passed.append(harness.check(
                    f"{src.name} compiles",
                    p.returncode == 0,
                    f"stderr: {p.stderr.strip()}",
                ))

            # ── STEP 2 — spin up Qdrant + stage fake $HOME ─────────────
            print(f"\nSTEP 2: spin up Qdrant :{QDRANT_PORT} + stage fake HOME")
            (tmp / "qdrant").mkdir()
            procs.append(harness.start_qdrant(QDRANT_PORT, tmp / "qdrant",
                                              logs / "qdrant.log"))
            harness.init_cowork_memories(QDRANT_PORT)
            print(f"  ✓ Qdrant ready ({core.COLLECTION} collection initialized)")

            fake_home = harness.stage_mem_fusion_home(tmp)
            print(f"  ✓ fake HOME at {fake_home}")

            # Pre-populate Qdrant with 3 seed memories so search hooks have hits.
            seed_ids = populate_seed_memories(qdrant_url)
            print(f"  ✓ 3 seed memories stored ({', '.join(i[:8] for i in seed_ids)})")
            passed.append(harness.check("seed memories visible to Qdrant",
                                        count_memories(qdrant_url) == 3))

            # Map for shorthand
            mf = fake_home / ".local/share/mem-fusion"

            # ── STEP 3 — SessionStart hook ─────────────────────────────
            print("\nSTEP 3: session_prime.sh (SessionStart) — expect <memfusion_status>")
            rc, out, err = run_hook(mf / "scripts/session_prime.sh",
                                    fake_home=fake_home)
            passed.append(harness.check("exit 0", rc == 0, f"stderr: {err[:200]}"))
            passed.append(harness.check("output starts with <memfusion_status>",
                                        out.lstrip().startswith("<memfusion_status>"),
                                        f"got: {out[:120]!r}"))
            passed.append(harness.check("output reports 3 memories stored",
                                        "3 memories stored" in out,
                                        f"output: {out[:200]!r}"))
            passed.append(harness.check("no AttributeError in stderr",
                                        "AttributeError" not in err,
                                        f"stderr: {err[:300]}"))

            # ── STEP 4 — UserPromptSubmit hook (matching prompt) ──────
            # The hook's 0.75 threshold is sensitive to embedding-model drift,
            # so we use a prompt empirically known to score ~0.80 against the
            # gRPC seed (well above 0.75, with margin). Don't tighten this to
            # a near-verbatim prompt — that turns the test into a tautology.
            print("\nSTEP 4: prompt_memory_inject.sh (UserPromptSubmit, matching prompt)")
            rc, out, err = run_hook(mf / "scripts/prompt_memory_inject.sh",
                                    fake_home=fake_home,
                                    stdin="What did we pick for internal RPC?")
            passed.append(harness.check("exit 0", rc == 0, f"stderr: {err[:200]}"))
            passed.append(harness.check("output contains <memory_context>",
                                        "<memory_context>" in out,
                                        f"got: {out[:200]!r}"))
            passed.append(harness.check("injects the gRPC memory",
                                        "gRPC" in out,
                                        f"got: {out[:300]!r}"))
            passed.append(harness.check("no AttributeError in stderr",
                                        "AttributeError" not in err,
                                        f"stderr: {err[:300]}"))

            # ── STEP 5 — UserPromptSubmit hook (short prompt → silent) ──
            print("\nSTEP 5: prompt_memory_inject.sh — short prompt (<15 chars) is silent")
            rc, out, err = run_hook(mf / "scripts/prompt_memory_inject.sh",
                                    fake_home=fake_home, stdin="hi")
            passed.append(harness.check("exit 0 (early return)", rc == 0))
            passed.append(harness.check("output is empty (no injection)",
                                        out.strip() == "", f"got: {out!r}"))

            # ── STEP 6 — UserPromptSubmit hook (unrelated prompt → silent) ──
            print("\nSTEP 6: prompt_memory_inject.sh — unrelated prompt scores below 0.75")
            rc, out, err = run_hook(
                mf / "scripts/prompt_memory_inject.sh",
                fake_home=fake_home,
                stdin="What is the airspeed velocity of an unladen swallow today?")
            passed.append(harness.check("exit 0", rc == 0))
            passed.append(harness.check(
                "no <memory_context> emitted for unrelated prompt",
                "<memory_context>" not in out, f"got: {out[:200]!r}"))

            # ── STEP 7 — PostToolUse:Write hook ────────────────────────
            print("\nSTEP 7: capture_file_write.sh (PostToolUse:Write) — fake 120-line file")
            sample = tmp / "sample_module.py"
            sample.write_text(
                "# sample module for hook test\n"
                + "\n".join(f"def fn_{i}(): return {i}" for i in range(120))
                + "\n"
            )
            before = count_memories(qdrant_url)
            rc, out, err = run_hook(
                mf / "scripts/capture_file_write.sh", fake_home=fake_home,
                stdin=json.dumps({"file_path": str(sample)}),
            )
            passed.append(harness.check("exit 0", rc == 0, f"stderr: {err[:200]}"))
            # capture_file_write.sh backgrounds the store via nohup — give it a moment.
            for _ in range(20):
                if count_memories(qdrant_url) > before:
                    break
                time.sleep(0.25)
            after = count_memories(qdrant_url)
            passed.append(harness.check(
                f"Qdrant gained 1 memory (before={before}, after={after})",
                after == before + 1))

            # Find the new memory and verify shape
            client = QdrantClient(url=qdrant_url, timeout=10)
            from qdrant_client.models import FieldCondition, Filter, MatchValue
            pts, _ = client.scroll(
                collection_name=core.COLLECTION,
                scroll_filter=Filter(must=[
                    FieldCondition(key="type", match=MatchValue(value="code")),
                ]),
                limit=10, with_payload=True,
            )
            if pts:
                pl = pts[0].payload or {}
                passed.append(harness.check("captured memory tagged type=code",
                                            pl.get("type") == "code"))
                passed.append(harness.check("captured memory groups=[personal]",
                                            pl.get("groups") == ["personal"],
                                            f"got groups={pl.get('groups')}"))
                passed.append(harness.check("captured memory tagged with file-write",
                                            "file-write" in pl.get("tags", []),
                                            f"got tags={pl.get('tags')}"))
            else:
                passed.append(harness.check("captured memory exists", False,
                                            "no type=code memory found"))

            # ── STEP 8 — Stop hook (ingest_session.py) ─────────────────
            print("\nSTEP 8: ingest_session.py (Stop) — synthesizes session memory")
            session_id = "test-session-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            write_fake_session(fake_home, session_id)
            before = count_memories(qdrant_url)
            venv_python = pathlib.Path.home() / ".local/share/cowork-memory/venv/bin/python"
            env = os.environ.copy()
            env["HOME"] = str(fake_home)
            env["QDRANT_URL"] = qdrant_url
            env["CLAUDE_PROJECT"] = "hook-test"
            p = subprocess.run(
                [str(venv_python), str(mf / "scripts/ingest_session.py")],
                env=env, capture_output=True, text=True, timeout=60,
            )
            passed.append(harness.check("Stop hook exit 0",
                                        p.returncode == 0,
                                        f"stderr: {p.stderr[:300]}"))
            after = count_memories(qdrant_url)
            delta = after - before
            passed.append(harness.check(
                f"Qdrant gained ≥1 memory from session ingest (delta={delta})",
                delta >= 1))

            # Verify the session-summary memory has groups=[personal]
            pts, _ = client.scroll(
                collection_name=core.COLLECTION,
                scroll_filter=Filter(must=[
                    FieldCondition(key="type", match=MatchValue(value="session")),
                ]),
                limit=10, with_payload=True,
            )
            if pts:
                pl = pts[0].payload or {}
                passed.append(harness.check(
                    "ingested session memory groups=[personal]",
                    pl.get("groups") == ["personal"],
                    f"got groups={pl.get('groups')}"))
            else:
                passed.append(harness.check(
                    "session-type memory exists after ingest", False))

            # Dedup registry should now contain the session id
            registry = mf / "queue/ingested_sessions.txt"
            passed.append(harness.check(
                "session id recorded in dedup registry",
                registry.exists() and session_id in registry.read_text()))

            # Re-run with same session: should be a no-op (dedup)
            before2 = count_memories(qdrant_url)
            subprocess.run(
                [str(venv_python), str(mf / "scripts/ingest_session.py")],
                env=env, capture_output=True, text=True, timeout=60,
            )
            after2 = count_memories(qdrant_url)
            passed.append(harness.check(
                "Stop hook is idempotent (no new memories on re-run)",
                after2 == before2,
                f"delta={after2 - before2}"))

            # ── Summary ────────────────────────────────────────────────
            ok, total = sum(passed), len(passed)
            print(f"\n  {ok}/{total} invariants passed")
            if ok != total:
                print("\n✗ Hook tests FAILED", file=sys.stderr)
                sys.exit(1)
            print("\n" + "=" * 70)
            print("✓ Hook tests PASSED")
            print("=" * 70)

        finally:
            for p in reversed(procs):
                harness.stop_proc(p)


if __name__ == "__main__":
    main()
