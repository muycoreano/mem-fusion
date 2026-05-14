#!/usr/bin/env python3
"""
Preload mem-fusion with 8 canonical usage-pattern memories so first-session
search returns substance — breaks the cold-start usefulness deadlock where
Claude inspects an empty store and silently routes around it.

All memories: groups=["personal"], tags=["onboarding", "usage-example"].
Idempotent: core.store_memory content-hashes on insert, so a second run
reports each as duplicate and writes nothing new.

Honors QDRANT_URL and OLLAMA_URL env vars (defaults match a standard install).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".local/share/mem-fusion"))
import core  # noqa: E402

SEEDS = [
    {
        "content": (
            "mem-fusion is the persistent semantic memory for this Claude install. "
            "Search it at session start; store decisions, errors, and preferences as "
            "they happen. The store grows with use — do not skip calls when it looks "
            "sparse; that is the failure mode that prevents it from ever filling."
        ),
        "type": "context", "importance": 4,
    },
    {
        "content": (
            "Session-start protocol: call memory_stats() to confirm the store is live, "
            "then search_memory(query='<current task or project name>', top_k=8) to "
            "surface prior context. Both calls are mandatory regardless of how full "
            "or sparse the store currently looks."
        ),
        "type": "context", "importance": 4,
    },
    {
        "content": (
            "store_memory importance levels: 5=explicit /remember, 4=decision or "
            "user preference, 3=novel error resolved or important context, "
            "2=routine context, 1=trivial. Use 3 as the default floor for "
            "autonomous stores — anything worth remembering at all is at least a 3."
        ),
        "type": "decision", "importance": 4,
    },
    {
        "content": (
            "Group routing is by explicit user intent, not content classification. "
            "Default groups=['personal'] for all hooks, silent stores, and /remember "
            "without an audience clause. Only widen the group set when the user "
            "explicitly names a group ('for engineering', 'share with product')."
        ),
        "type": "decision", "importance": 4,
    },
    {
        "content": (
            "Two-store architecture: file-based ~/.claude/projects/.../memory/*.md "
            "holds always-loaded behavioral rules; Qdrant via mem-fusion holds "
            "searchable semantic memory. Most content lives in one store or the "
            "other, not both — file-based for rules Claude needs every session, "
            "Qdrant for facts retrievable on demand."
        ),
        "type": "context", "importance": 4,
    },
    {
        "content": (
            "When the user says 'let's pause and reflect' or asks for a session "
            "summary, store 3-5 memories covering decisions made, errors resolved, "
            "preferences revealed, and context that may matter later. Default "
            "groups=['personal']; user can widen retroactively with 'share those "
            "with X' which triggers add_groups + group_push for that group only."
        ),
        "type": "preference", "importance": 4,
    },
    {
        "content": (
            "mem-fusion MCP tool surface: store_memory, search_memory, search_recent, "
            "memory_stats, find_or_create, upsert_memory, delete_memory, get_related, "
            "add_groups, export_record. Plus group_pull and group_push when "
            "Constellation is installed. All tools are namespaced mcp__mem-fusion__*."
        ),
        "type": "context", "importance": 3,
    },
    {
        "content": (
            "Verification rule: memories are point-in-time observations. Before "
            "recommending a file path, function name, or flag named in a recalled "
            "memory, verify it still exists in the current code via grep or Read. "
            "Memories can become stale; the code is authoritative."
        ),
        "type": "preference", "importance": 4,
    },
]


async def main() -> int:
    stored = duplicate = merged = 0
    for seed in SEEDS:
        args = dict(seed)
        args["project"] = "mem-fusion-onboarding"
        args["tags"]    = ["onboarding", "usage-example"]
        args["groups"]  = ["personal"]
        result = await core.store_memory(args)
        status = result.get("status", "unknown")
        if status == "stored":
            stored += 1
        elif status == "duplicate":
            duplicate += 1
        elif status == "merged":
            merged += 1
        print(f"  [{status:9s}] {seed['content'][:72]}…")

    print(f"\n✓ Preload complete — {stored} stored, {duplicate} duplicate, {merged} merged (of {len(SEEDS)} seeds)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
