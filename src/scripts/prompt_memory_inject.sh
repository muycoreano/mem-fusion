#!/usr/bin/env bash
# UserPromptSubmit hook — injects relevant memories above 0.75 score.
VENV="$HOME/.local/share/mem-fusion/venv/bin/python"
QUEUE="$HOME/.local/share/mem-fusion/queue"
SESSION_ID="${CLAUDE_SESSION_ID:-unknown}"
SEEN_FILE="${QUEUE}/seen-${SESSION_ID}.txt"

PROMPT=$(cat)
[[ ${#PROMPT} -lt 15 ]] && exit 0

$VENV - <<PYEOF
import sys, asyncio, os
sys.path.insert(0, "$HOME/.local/share/mem-fusion")
import core

PROMPT    = """${PROMPT//\"/\\\"}"""
SEEN_FILE = """${SEEN_FILE}"""
MAX_CHARS = 2000
MIN_SCORE = 0.75

async def main():
    seen_ids = set()
    try:
        with open(SEEN_FILE) as f:
            seen_ids = set(f.read().splitlines())
    except FileNotFoundError:
        pass

    result = await core.search_memory({"query": PROMPT, "top_k": 5, "min_importance": 2})
    hits = [r for r in result.get("results", [])
            if r["score"] >= MIN_SCORE and r["id"] not in seen_ids]
    if not hits:
        return

    lines, total, new_ids = [], 0, []
    for hit in hits:
        entry = f'<memory id="{hit["id"]}" score="{hit["score"]}" type="{hit["type"]}">{hit["content"]}</memory>'
        if total + len(entry) > MAX_CHARS:
            break
        lines.append(entry)
        total += len(entry)
        new_ids.append(hit["id"])

    if not lines:
        return

    with open(SEEN_FILE, "a") as f:
        f.write("\n".join(new_ids) + "\n")

    print("<memory_context>\n" + "\n".join(lines) + "\n</memory_context>")

asyncio.run(main())
PYEOF
