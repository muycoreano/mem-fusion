#!/usr/bin/env bash
# SessionStart hook — pre-loads project-relevant memories into context.
#
# Stdout (both blocks are injected as session context by Claude Code):
#   <memfusion_status>   one-line liveness banner (total / last_stored)
#   <memory_context>     full memory entries, same envelope used by
#                        prompt_memory_inject.sh per user prompt
#
# Query is derived from cwd basename + git branch + last 3 commit subjects,
# combined with a 72h search_recent pull. Min importance for the
# project-scoped pull is 3; recents are unfiltered. Total content capped
# at MAX_CHARS to bound session-start token cost.
#
# Stdin contract (Claude Code SessionStart hook):
#   {"session_id":"<uuid>","cwd":"<path>","source":"startup|resume|clear",
#    "hook_event_name":"SessionStart", ...}

VENV="$HOME/.local/share/mem-fusion/venv/bin/python"
HOOK_JSON="$(cat 2>/dev/null || true)"

# Resolve cwd from stdin JSON; fall back to $PWD.
CWD=$(HOOK_JSON="$HOOK_JSON" "$VENV" - <<'PYEOF' 2>/dev/null
import os, json
try:
    d = json.loads(os.environ.get("HOOK_JSON", ""))
    print(d.get("cwd") or os.environ.get("PWD", ""))
except Exception:
    print(os.environ.get("PWD", ""))
PYEOF
)
CWD="${CWD:-$PWD}"

# Best-effort git context (tolerates non-git cwds).
BRANCH=""
COMMITS=""
if git -C "$CWD" rev-parse --git-dir >/dev/null 2>&1; then
  BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  COMMITS=$(git -C "$CWD" log -3 --pretty=format:'%s' 2>/dev/null | tr '\n' ' ' || true)
fi

PROJ=$(basename "$CWD")
QUERY="${PROJ} ${BRANCH} ${COMMITS}"

QUERY="$QUERY" "$VENV" - <<'PYEOF'
import os, sys, asyncio
sys.path.insert(0, os.path.expanduser("~/.local/share/mem-fusion"))
import core

QUERY = os.environ.get("QUERY", "").strip() or "current project"
MAX_CHARS = 3000

async def main():
    stats = await core.memory_stats({})
    total = stats.get("total_memories", 0)
    last = stats.get("last_stored", "none")

    projhits = (await core.search_memory({
        "query": QUERY, "top_k": 6, "min_importance": 3,
    })).get("results", [])
    recents = (await core.search_recent({
        "hours": 72, "top_k": 5,
    })).get("results", [])

    seen, combined = set(), []
    for h in projhits + recents:
        hid = h.get("id")
        if not hid or hid in seen:
            continue
        seen.add(hid)
        combined.append(h)

    print("<memfusion_status>")
    print(f"  Vector memory: {total} memories stored | Last update: {last}")
    print("  Session-start context pre-loaded below; call search_memory only for topics not covered.")
    print("</memfusion_status>")

    if not combined:
        return

    out, used = [], 0
    for h in combined:
        entry = (
            f'<memory id="{h.get("id","")}" score="{h.get("score","")}" '
            f'type="{h.get("type","")}">{h.get("content","")}</memory>'
        )
        if used + len(entry) > MAX_CHARS:
            break
        out.append(entry)
        used += len(entry)

    if out:
        print("<memory_context>")
        print("\n".join(out))
        print("</memory_context>")

asyncio.run(main())
PYEOF
