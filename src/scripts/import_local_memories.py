#!/usr/bin/env python3
"""
Import Claude's file-based auto-memory into mem-fusion as personal memories.

Scans ~/.claude/projects/*/memory/*.md for memory files (skipping MEMORY.md,
which is an index, not a memory). Parses each file's YAML-style frontmatter
and body. Stores each into Qdrant via core.store_memory with:

  - content : the body text
  - type    : frontmatter.metadata.type (or top-level type, fallback "context")
  - tags    : ["imported-from-file-memory"] + the frontmatter.name as a tag
  - project : "mem-fusion-onboarding"
  - groups  : ["personal"]
  - importance: 4

Idempotent — core.store_memory content-hashes on insert; a re-run reports
each as duplicate and writes nothing new.

This is a one-shot bootstrap seed, not an ongoing sync. The two stores stay
separate per the dual-memory rule; this just gives the vector DB substance
from real personal content on first install.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".local/share/mem-fusion"))
import core  # noqa: E402

PROJECTS_ROOT = Path.home() / ".claude/projects"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML-frontmatter parser: handles `key: value` and `key:` indented blocks.

    Returns (metadata_dict, body_text). If the file has no frontmatter
    fence, returns ({}, text).
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    block = text[4:end]
    body  = text[end + 5:]

    meta: dict = {}
    current_parent: str | None = None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith("  ") and current_parent:
            sub = raw.strip()
            if ":" in sub:
                k, _, v = sub.partition(":")
                meta.setdefault(current_parent, {})[k.strip()] = v.strip()
            continue
        if ":" in raw:
            k, _, v = raw.partition(":")
            k = k.strip()
            v = v.strip()
            if v == "":
                current_parent = k
                meta[k] = {}
            else:
                current_parent = None
                meta[k] = v
    return meta, body


def memory_type(meta: dict) -> str:
    md = meta.get("metadata")
    if isinstance(md, dict) and md.get("type"):
        return str(md["type"])
    if meta.get("type"):
        return str(meta["type"])
    return "context"


async def main() -> int:
    if not PROJECTS_ROOT.is_dir():
        print(f"No {PROJECTS_ROOT} — nothing to import.")
        return 0

    md_files = []
    for project_dir in sorted(PROJECTS_ROOT.iterdir()):
        mem_dir = project_dir / "memory"
        if not mem_dir.is_dir():
            continue
        for f in sorted(mem_dir.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            md_files.append(f)

    if not md_files:
        print(f"No memory files found under {PROJECTS_ROOT}/*/memory/ — nothing to import.")
        return 0

    stored = duplicate = merged = skipped = 0
    for f in md_files:
        try:
            text = f.read_text()
        except OSError as e:
            print(f"  [skip-read   ] {f.name}: {e}")
            skipped += 1
            continue

        meta, body = parse_frontmatter(text)
        body = body.strip()
        if not body:
            print(f"  [skip-empty  ] {f.name}: no body content")
            skipped += 1
            continue

        name = meta.get("name", f.stem)
        args = {
            "content":    body,
            "type":       memory_type(meta),
            "importance": 4,
            "project":    "mem-fusion-onboarding",
            "tags":       ["imported-from-file-memory", str(name)],
            "groups":     ["personal"],
        }
        result = await core.store_memory(args)
        status = result.get("status", "unknown")
        if status == "stored":
            stored += 1
        elif status == "duplicate":
            duplicate += 1
        elif status == "merged":
            merged += 1
        print(f"  [{status:9s}] {f.name}")

    print(
        f"\n✓ Import complete — {stored} stored, {duplicate} duplicate, "
        f"{merged} merged, {skipped} skipped (of {len(md_files)} files)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
