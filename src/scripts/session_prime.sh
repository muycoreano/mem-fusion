#!/usr/bin/env bash
# SessionStart hook — emits a brief recent-memory snapshot for context priming.
VENV="$HOME/.local/share/mem-fusion/venv/bin/python"

$VENV - <<PYEOF
import sys, asyncio
sys.path.insert(0, "$HOME/.local/share/mem-fusion")
import mem_fusion as srv

async def main():
    recent = await srv.tool_search_recent({"hours": 48, "top_k": 5})
    stats  = await srv.tool_stats({})
    total  = stats.get("total_memories", 0)
    last   = stats.get("last_stored", "none")

    lines = [
        "<memfusion_status>",
        f"  Vector memory: {total} memories stored | Last update: {last}",
    ]
    if recent.get("count", 0) > 0:
        lines.append("  Recent activity (last 48h):")
        for r in recent["results"][:3]:
            ts    = r.get("timestamp", "")[:10]
            ctype = r.get("type", "")
            proj  = r.get("project", "")
            snip  = r.get("content", "")[:80].replace("\n", " ")
            lines.append(f"    [{ts}] [{ctype}] {proj}: {snip}…")
    else:
        lines.append("  No recent activity in last 48h.")
    lines.append("  Use search_memory(query=...) to retrieve relevant context.")
    lines.append("</memfusion_status>")
    print("\n".join(lines))

asyncio.run(main())
PYEOF
