#!/usr/bin/env bash
set -e
launchctl list | grep -E "com.branchapp.memfusion" | head
curl -s http://127.0.0.1:6333/healthz
curl -s http://127.0.0.1:11434/api/tags | python3 -c "import sys,json; print('Ollama models:', [m['name'] for m in json.load(sys.stdin)['models']])"

claude mcp list | grep mem-fusion

~/.local/share/mem-fusion/venv/bin/python - <<'PYEOF'
import asyncio, sys
sys.path.insert(0, str(__import__('pathlib').Path.home() / ".local/share/mem-fusion"))
import core

async def main():
    r1 = await core.store_memory({
        "content": "Smoke test: Mem-Fusion install completed on " + __import__('datetime').datetime.now().isoformat(),
        "type": "context", "importance": 3, "project": "install-test",
        "tags": ["smoke-test"], "groups": ["personal"],
    })
    print("STORE:", r1)

    r2 = await core.search_memory({"query": "smoke test install", "top_k": 3})
    print("SEARCH count:", r2["count"])
    assert r2["count"] >= 1, "Search didn't find the just-stored memory"

    r3 = await core.memory_stats({})
    print("STATS:", r3)

    print("\n✓ All smoke tests passed")

asyncio.run(main())
PYEOF
