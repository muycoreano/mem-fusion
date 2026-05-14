#!/usr/bin/env bash
# Defaults match a standard install; tests override these to point at a
# tempdir-scoped Qdrant. core.py inside the heredoc honors QDRANT_URL/OLLAMA_URL
# the same way (module-level os.getenv with the same defaults).
set -e
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"

launchctl list | grep -E "com.branchapp.memfusion" | head
curl -s "$QDRANT_URL/healthz"
curl -s "$OLLAMA_URL/api/tags" | python3 -c "import sys,json; print('Ollama models:', [m['name'] for m in json.load(sys.stdin)['models']])"

# Best-effort MCP-registration check. Skipped in environments where the
# claude CLI isn't installed (CI, tests); demoted to a warning if mem-fusion
# isn't yet registered (which is the case during partial installs).
if command -v claude >/dev/null 2>&1; then
    claude mcp list | grep mem-fusion || echo "WARN: mem-fusion not registered with claude CLI"
else
    echo "WARN: claude CLI not found; skipping MCP registration check"
fi

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
