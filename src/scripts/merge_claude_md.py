#!/usr/bin/env python3
"""
Merge the canonical mem-fusion Vector Memory System section into ~/CLAUDE.md.

Reads the snippet from MEMFUSION_SNIPPET (default: ~/.local/share/mem-fusion/
claude_md_snippet.md), finds any existing `## Vector Memory System` section
in ~/CLAUDE.md, replaces it with the snippet, leaves everything else untouched.
If no such section exists, appends. If no ~/CLAUDE.md exists, creates one.

Idempotent: a second run with the same inputs is a no-op.

Backs up ~/CLAUDE.md to ~/CLAUDE.md.bak.<UTC-ISO-timestamp> only when content
actually changes.
"""
import os
import sys
import datetime
from pathlib import Path

HOME            = Path.home()
CLAUDE_MD       = HOME / "CLAUDE.md"
DEFAULT_SNIPPET = HOME / ".local/share/mem-fusion/claude_md_snippet.md"
SNIPPET_PATH    = Path(os.getenv("MEMFUSION_SNIPPET", str(DEFAULT_SNIPPET)))
SECTION_HEADER  = "## Vector Memory System"


def section_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Return (start, end_exclusive) of an existing Vector Memory System section, or None."""
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == SECTION_HEADER:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") and not lines[j].startswith("### "):
            end = j
            break
    return (start, end)


def build_merged(existing: str, snippet: str) -> str:
    snippet = snippet.rstrip() + "\n"
    if not existing:
        return snippet
    lines = existing.splitlines(keepends=True)
    bounds = section_bounds(lines)
    if bounds is None:
        prefix = existing if existing.endswith("\n") else existing + "\n"
        sep = "" if prefix.endswith("\n\n") else "\n"
        return prefix + sep + snippet
    start, end = bounds
    before = "".join(lines[:start])
    after  = "".join(lines[end:])
    if before and not before.endswith("\n"):
        before += "\n"
    if after and not after.startswith("\n"):
        after = "\n" + after
    return before + snippet + after


def main() -> int:
    if not SNIPPET_PATH.is_file():
        print(f"ERROR: snippet not found at {SNIPPET_PATH}", file=sys.stderr)
        return 1
    snippet = SNIPPET_PATH.read_text()

    existing = CLAUDE_MD.read_text() if CLAUDE_MD.exists() else ""
    merged   = build_merged(existing, snippet)

    if merged == existing:
        print(f"~/CLAUDE.md already current — no changes.")
        return 0

    if existing:
        ts     = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = CLAUDE_MD.with_suffix(CLAUDE_MD.suffix + f".bak.{ts}")
        backup.write_text(existing)
        print(f"Backed up existing ~/CLAUDE.md → {backup.name}")

    CLAUDE_MD.write_text(merged)
    action = "Created" if not existing else ("Replaced section in" if section_bounds(existing.splitlines(keepends=True)) else "Appended to")
    print(f"{action} ~/CLAUDE.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
