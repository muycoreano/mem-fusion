#!/usr/bin/env bash
# Captures NEW files >100 lines as code memories. Fire-and-forget, backgrounded.
VENV="$HOME/.local/share/mem-fusion/venv/bin/python"
LOG="$HOME/.local/share/mem-fusion/logs/ingest.log"

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('file_path',''))" 2>/dev/null)

[[ -z "$FILE_PATH" || ! -f "$FILE_PATH" ]] && exit 0

LINE_COUNT=$(wc -l < "$FILE_PATH" 2>/dev/null || echo 0)
[[ "$LINE_COUNT" -lt 100 ]] && exit 0

PROJECT=$(basename "$(pwd)")

nohup $VENV - <<PYEOF >> "$LOG" 2>&1 &
import sys, asyncio
sys.path.insert(0, "$HOME/.local/share/mem-fusion")
import core

async def main():
    path  = """${FILE_PATH}"""
    lines = ${LINE_COUNT}
    proj  = """${PROJECT}"""
    with open(path) as f:
        head = "".join(f.readlines()[:10]).strip()[:300]
    content = f"New file written: {path} ({lines} lines)\n\nHeader:\n{head}"
    result = await core.store_memory({
        "content": content, "type": "code", "project": proj,
        "importance": 3, "tags": ["file-write"],
        "groups": ["personal"],
    })
    print(f"capture_file_write: {result}")

asyncio.run(main())
PYEOF

exit 0
