#!/usr/bin/env python3
"""
End-to-end coverage for the three POST_INSTALL scripts:

  - src/scripts/merge_claude_md.py         — idempotent merge into ~/CLAUDE.md
  - src/scripts/preload_usage_memories.py  — 8 seed memories into Qdrant
  - src/scripts/import_local_memories.py   — import file-based memories

Each is exercised against a tempdir-scoped Qdrant + fake $HOME via
tests/lib/harness.py. Idempotency is verified by running each script
twice and checking that the second run reports duplicates / no-op.

Usage:
  ~/.local/share/cowork-memory/venv/bin/python tests/installer/test-post-install-scripts.py
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


QDRANT_PORT = 6791
SCRIPTS_DIR = REPO_ROOT / "src/scripts"
SNIPPET_SRC = REPO_ROOT / "src/config/claude_md_snippet.md"


def run(script: pathlib.Path, fake_home: pathlib.Path,
        env_extra: dict | None = None,
        timeout_s: float = 60.0) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["HOME"]       = str(fake_home)
    env["QDRANT_URL"] = f"http://127.0.0.1:{QDRANT_PORT}"
    if env_extra:
        env.update(env_extra)
    venv_py = pathlib.Path.home() / ".local/share/cowork-memory/venv/bin/python"
    p = subprocess.run(
        [str(venv_py), str(script)],
        env=env, capture_output=True, text=True, timeout=timeout_s,
    )
    return p.returncode, p.stdout, p.stderr


def main() -> None:
    print("=" * 70)
    print("POST_INSTALL scripts — end-to-end coverage")
    print("=" * 70)

    if not harness.QDRANT_BIN.exists():
        print(f"✗ missing qdrant binary at {harness.QDRANT_BIN}", file=sys.stderr)
        sys.exit(1)

    passed: list[bool] = []
    procs: list = []

    with tempfile.TemporaryDirectory(prefix="mem-fusion-post-install-") as tdir:
        tmp = pathlib.Path(tdir)
        logs = tmp / "logs"; logs.mkdir()
        try:
            # ── compile-check all three ────────────────────────────────
            print("\nSTEP 1: python -c compile-check on all three scripts")
            for s in ("merge_claude_md.py", "preload_usage_memories.py",
                      "import_local_memories.py"):
                src_path = SCRIPTS_DIR / s
                p = subprocess.run(
                    ["python3", "-c", f"compile(open({str(src_path)!r}).read(), {str(src_path)!r}, 'exec')"],
                    capture_output=True, text=True,
                )
                passed.append(harness.check(
                    f"{s} compiles", p.returncode == 0,
                    f"stderr: {p.stderr.strip()}"))

            # ── spin up Qdrant + fake HOME ─────────────────────────────
            print(f"\nSTEP 2: spin up Qdrant :{QDRANT_PORT} + stage fake $HOME")
            (tmp / "qdrant").mkdir()
            procs.append(harness.start_qdrant(
                QDRANT_PORT, tmp / "qdrant", logs / "qdrant.log"))
            harness.init_cowork_memories(QDRANT_PORT)
            fake_home = harness.stage_mem_fusion_home(tmp)
            # Stage the canonical snippet where merge_claude_md.py expects it
            (fake_home / ".local/share/mem-fusion/claude_md_snippet.md").write_bytes(
                SNIPPET_SRC.read_bytes())
            print(f"  ✓ Qdrant ready, fake HOME at {fake_home}")

            # ── merge_claude_md.py — case A: no existing CLAUDE.md ─────
            print("\nSTEP 3: merge_claude_md.py — creates ~/CLAUDE.md from scratch")
            claude_md = fake_home / "CLAUDE.md"
            rc, out, err = run(SCRIPTS_DIR / "merge_claude_md.py", fake_home)
            passed.append(harness.check(
                "merge (create) exit 0", rc == 0,
                f"stderr: {err[:300]}"))
            passed.append(harness.check(
                "~/CLAUDE.md created with snippet header",
                claude_md.exists() and "## Vector Memory System" in claude_md.read_text(),
                f"exists={claude_md.exists()}"))
            passed.append(harness.check(
                "~/CLAUDE.md contains the strengthened ALWAYS-USE directive",
                "Always use mem-fusion, regardless of how full or sparse" in claude_md.read_text(),
                ""))

            # ── merge_claude_md.py — case B: existing section replaced ─
            print("\nSTEP 4: merge_claude_md.py — replaces existing section, preserves rest")
            claude_md.write_text(
                "# My Notes\n\n"
                "## Some other section\n\nKeep me.\n\n"
                "## Vector Memory System\n\nOld content here.\n\n"
                "## Tail section\n\nKeep me too.\n"
            )
            rc, out, err = run(SCRIPTS_DIR / "merge_claude_md.py", fake_home)
            merged = claude_md.read_text()
            passed.append(harness.check(
                "merge (replace) exit 0", rc == 0, f"stderr: {err[:300]}"))
            passed.append(harness.check(
                "other sections preserved",
                "## Some other section" in merged and "## Tail section" in merged
                and "Keep me." in merged and "Keep me too." in merged,
                ""))
            passed.append(harness.check(
                "old Vector Memory System content removed",
                "Old content here." not in merged, ""))
            passed.append(harness.check(
                "backup file created on replace",
                any(f.name.startswith("CLAUDE.md.bak.") for f in fake_home.iterdir()),
                f"siblings: {[f.name for f in fake_home.iterdir()]}"))

            # ── merge_claude_md.py — case C: idempotent (second run = no-op) ─
            print("\nSTEP 5: merge_claude_md.py — second run is a no-op")
            backups_before = {f.name for f in fake_home.iterdir() if f.name.startswith("CLAUDE.md.bak.")}
            rc, out, err = run(SCRIPTS_DIR / "merge_claude_md.py", fake_home)
            backups_after = {f.name for f in fake_home.iterdir() if f.name.startswith("CLAUDE.md.bak.")}
            passed.append(harness.check(
                "second-run exit 0", rc == 0, f"stderr: {err[:300]}"))
            passed.append(harness.check(
                "second run created no new backup",
                backups_before == backups_after,
                f"before={backups_before}, after={backups_after}"))
            passed.append(harness.check(
                "second-run stdout reports 'already current'",
                "already current" in out, f"got: {out!r}"))

            # ── preload_usage_memories.py — first + second run ─────────
            print("\nSTEP 6: preload_usage_memories.py — stores 8 then duplicates")
            rc, out, err = run(SCRIPTS_DIR / "preload_usage_memories.py", fake_home)
            passed.append(harness.check(
                "preload (first) exit 0", rc == 0, f"stderr: {err[:300]}"))
            passed.append(harness.check(
                "preload reports 8 stored",
                "8 stored" in out, f"got tail: {out[-300:]!r}"))

            rc, out, err = run(SCRIPTS_DIR / "preload_usage_memories.py", fake_home)
            passed.append(harness.check(
                "preload (second) exit 0", rc == 0, f"stderr: {err[:300]}"))
            passed.append(harness.check(
                "preload (second) reports 8 duplicate",
                "8 duplicate" in out or "0 stored" in out,
                f"got tail: {out[-300:]!r}"))

            # Verify in Qdrant: ≥8 memories with tags=['onboarding','usage-example']
            from qdrant_client import QdrantClient
            from qdrant_client.models import FieldCondition, Filter, MatchValue
            client = QdrantClient(url=f"http://127.0.0.1:{QDRANT_PORT}", timeout=10)
            pts, _ = client.scroll(
                collection_name=core.COLLECTION,
                scroll_filter=Filter(must=[
                    FieldCondition(key="tags", match=MatchValue(value="onboarding")),
                ]),
                limit=20, with_payload=True,
            )
            passed.append(harness.check(
                "≥8 onboarding-tagged memories in Qdrant",
                len(pts) >= 8, f"got {len(pts)}"))
            passed.append(harness.check(
                "all preload memories tagged groups=['personal']",
                all((p.payload or {}).get("groups") == ["personal"] for p in pts),
                f"got groups: {[(p.payload or {}).get('groups') for p in pts]}"))

            # ── import_local_memories.py — set up fake projects + run ──
            print("\nSTEP 7: import_local_memories.py — imports 2 files, skips MEMORY.md")
            proj = fake_home / ".claude/projects/-fake-proj/memory"
            proj.mkdir(parents=True)
            (proj / "MEMORY.md").write_text(
                "# Index — do not import\nThis is the index, must be skipped.")
            (proj / "feedback_test.md").write_text(
                "---\nname: test-feedback\ntype: feedback\n"
                "description: Test feedback for import\n---\n"
                "This is a test feedback memory body for the importer.\n")
            (proj / "context_test.md").write_text(
                "---\nname: test-context\nmetadata:\n  type: context\n---\n"
                "Test context body. Should import as type=context per metadata.type.\n")

            rc, out, err = run(SCRIPTS_DIR / "import_local_memories.py", fake_home)
            passed.append(harness.check(
                "import (first) exit 0", rc == 0, f"stderr: {err[:300]}"))
            passed.append(harness.check(
                "import reports 2 stored",
                "2 stored" in out, f"got tail: {out[-300:]!r}"))
            passed.append(harness.check(
                "MEMORY.md was skipped (not listed in stored output)",
                "MEMORY.md" not in out, f"got: {out[-400:]!r}"))

            rc, out, err = run(SCRIPTS_DIR / "import_local_memories.py", fake_home)
            passed.append(harness.check(
                "import (second) exit 0", rc == 0, f"stderr: {err[:300]}"))
            passed.append(harness.check(
                "import (second) reports 2 duplicate",
                "2 duplicate" in out or "0 stored" in out,
                f"got tail: {out[-300:]!r}"))

            # Verify in Qdrant: imported memories carry the right tags + types
            pts, _ = client.scroll(
                collection_name=core.COLLECTION,
                scroll_filter=Filter(must=[
                    FieldCondition(key="tags",
                                   match=MatchValue(value="imported-from-file-memory")),
                ]),
                limit=20, with_payload=True,
            )
            passed.append(harness.check(
                "≥2 imported-from-file-memory memories in Qdrant",
                len(pts) >= 2, f"got {len(pts)}"))
            types = {(p.payload or {}).get("type") for p in pts}
            passed.append(harness.check(
                "imported types preserved from frontmatter",
                "feedback" in types and "context" in types,
                f"got types={types}"))

            # ── Summary ────────────────────────────────────────────────
            ok, total = sum(passed), len(passed)
            print(f"\n  {ok}/{total} invariants passed")
            if ok != total:
                print("\n✗ POST_INSTALL scripts coverage FAILED", file=sys.stderr)
                sys.exit(1)
            print("\n" + "=" * 70)
            print("✓ POST_INSTALL scripts coverage PASSED")
            print("=" * 70)

        finally:
            for p in reversed(procs):
                harness.stop_proc(p)


if __name__ == "__main__":
    main()
