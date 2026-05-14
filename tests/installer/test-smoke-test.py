#!/usr/bin/env python3
"""
Test coverage for src/scripts/smoke-test.sh — the install-time verification
script that runs at INSTALL_MEM_FUSION.md Step 14.

This script is part of the install path: every fresh v0.4 install runs it
as the final go/no-go check. The script embeds a Python heredoc that
imports `core` and exercises store/search/stats. The previous shape used
`mem_fusion as srv` + `srv.tool_*` — the v0.3 cowork-memory pattern that
doesn't exist on mem-fusion's surface. That bug shipped because no test
ever ran the embedded Python end-to-end; bash -n only checks shell syntax,
the heredoc body is opaque to it.

Coverage here:
  - Compile-check (bash -n)
  - End-to-end run against a tempdir Qdrant + staged fake $HOME
  - Assert exit 0, expected success line, and no AttributeError / no
    "command not found" anywhere in output

The hardened smoke-test.sh honors QDRANT_URL / OLLAMA_URL env vars and
makes the `claude mcp list` check best-effort, so it can run cleanly in
a test environment without the claude CLI.

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/installer/test-smoke-test.py
"""
import os
import pathlib
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "lib"))
import core      # noqa: E402
import harness   # noqa: E402


QDRANT_PORT = 6783
SMOKE_SH    = REPO_ROOT / "src/scripts/smoke-test.sh"


def main() -> None:
    print("=" * 70)
    print("smoke-test.sh — install-time verification script coverage")
    print("=" * 70)

    if not harness.QDRANT_BIN.exists():
        print(f"✗ missing qdrant binary at {harness.QDRANT_BIN}", file=sys.stderr)
        sys.exit(1)

    passed: list[bool] = []
    procs: list = []

    with tempfile.TemporaryDirectory(prefix="mem-fusion-smoke-") as tdir:
        tmp = pathlib.Path(tdir)
        logs = tmp / "logs"; logs.mkdir()
        try:
            # ── STEP 1 — compile-check ─────────────────────────────────
            print("\nSTEP 1: bash -n smoke-test.sh")
            p = subprocess.run(["bash", "-n", str(SMOKE_SH)],
                               capture_output=True, text=True)
            passed.append(harness.check(
                "smoke-test.sh compiles",
                p.returncode == 0, f"stderr: {p.stderr.strip()}",
            ))

            # ── STEP 2 — spin up Qdrant + stage fake HOME ──────────────
            print(f"\nSTEP 2: spin up Qdrant :{QDRANT_PORT} + stage fake $HOME")
            (tmp / "qdrant").mkdir()
            procs.append(harness.start_qdrant(
                QDRANT_PORT, tmp / "qdrant", logs / "qdrant.log"))
            harness.init_cowork_memories(QDRANT_PORT)
            fake_home = harness.stage_mem_fusion_home(tmp)
            print(f"  ✓ Qdrant ready, fake HOME at {fake_home}")

            # ── STEP 3 — run smoke-test.sh against the test environment ──
            print("\nSTEP 3: run smoke-test.sh with QDRANT_URL overridden to test port")
            env = os.environ.copy()
            env["HOME"]       = str(fake_home)
            env["QDRANT_URL"] = f"http://127.0.0.1:{QDRANT_PORT}"
            # OLLAMA_URL unset → script uses default (host's real Ollama),
            # same as all other tests that need real embeddings.

            p = subprocess.run(
                [str(SMOKE_SH)], env=env,
                capture_output=True, text=True, timeout=60,
            )
            out = p.stdout
            err = p.stderr

            passed.append(harness.check(
                "smoke-test.sh exit 0", p.returncode == 0,
                f"stderr: {err[:400]}\nstdout: {out[:200]}",
            ))
            passed.append(harness.check(
                "output contains '✓ All smoke tests passed'",
                "✓ All smoke tests passed" in out,
                f"got: {out[-200:]!r}",
            ))
            passed.append(harness.check(
                "output reports a STORE result",
                "STORE:" in out and "'status': 'stored'" in out,
                f"got: {out[:400]!r}",
            ))
            passed.append(harness.check(
                "output reports a SEARCH count",
                "SEARCH count:" in out,
                f"got: {out[:400]!r}",
            ))
            passed.append(harness.check(
                "output reports STATS with total_memories",
                "STATS:" in out and "total_memories" in out,
                f"got: {out[-300:]!r}",
            ))

            # The critical regression checks: catch the class of bug that
            # has shipped twice already.
            passed.append(harness.check(
                "no AttributeError (catches srv.tool_* and similar)",
                "AttributeError" not in out and "AttributeError" not in err,
                f"stdout: {out[-400:]}\nstderr: {err[-400:]}",
            ))
            passed.append(harness.check(
                "no 'command not found' (catches missing-CLI brittleness)",
                "command not found" not in err,
                f"stderr: {err[-400:]}",
            ))
            passed.append(harness.check(
                "no ImportError",
                "ImportError" not in out and "ImportError" not in err,
                f"stderr: {err[-400:]}",
            ))

            # ── STEP 4 — verify the stored memory is in Qdrant ─────────
            print("\nSTEP 4: verify the smoke-test memory landed in the test Qdrant")
            from qdrant_client import QdrantClient
            from qdrant_client.models import FieldCondition, Filter, MatchValue
            client = QdrantClient(url=f"http://127.0.0.1:{QDRANT_PORT}", timeout=10)
            pts, _ = client.scroll(
                collection_name=core.COLLECTION,
                scroll_filter=Filter(must=[
                    FieldCondition(key="project",
                                   match=MatchValue(value="install-test")),
                ]),
                limit=5, with_payload=True,
            )
            passed.append(harness.check(
                "smoke-test memory exists in test Qdrant",
                len(pts) >= 1, f"got {len(pts)} matching points"))
            if pts:
                pl = pts[0].payload or {}
                passed.append(harness.check(
                    "smoke-test memory tagged groups=[personal]",
                    pl.get("groups") == ["personal"],
                    f"got groups={pl.get('groups')}"))
                passed.append(harness.check(
                    "smoke-test memory has tags=['smoke-test']",
                    "smoke-test" in (pl.get("tags") or []),
                    f"got tags={pl.get('tags')}"))

            # ── Summary ────────────────────────────────────────────────
            ok, total = sum(passed), len(passed)
            print(f"\n  {ok}/{total} invariants passed")
            if ok != total:
                print("\n--- smoke-test.sh stdout ---", file=sys.stderr)
                print(out, file=sys.stderr)
                print("\n--- smoke-test.sh stderr ---", file=sys.stderr)
                print(err, file=sys.stderr)
                print("\n✗ smoke-test.sh coverage FAILED", file=sys.stderr)
                sys.exit(1)
            print("\n" + "=" * 70)
            print("✓ smoke-test.sh coverage PASSED")
            print("=" * 70)

        finally:
            for p in reversed(procs):
                harness.stop_proc(p)


if __name__ == "__main__":
    main()
