#!/usr/bin/env bash
# UserPromptSubmit hook — two passes:
#   1. Inject memories semantically relevant to the user prompt (existing).
#   2. Notify the user of memories received from peer nodes since the last
#      prompt in this session (new in v0.5).
#
# Both passes write to stdout; Claude picks them up as context blocks.
# Pass 1 outputs <memory_context>; pass 2 outputs <received_memories>.

VENV="$HOME/.local/share/mem-fusion/venv/bin/python"
QUEUE="$HOME/.local/share/mem-fusion/queue"
SESSION_ID="${CLAUDE_SESSION_ID:-unknown}"
SEEN_FILE="${QUEUE}/seen-${SESSION_ID}.txt"
LAST_TS_FILE="${QUEUE}/last-prompt-ts-${SESSION_ID}.txt"

PROMPT=$(cat)
[[ ${#PROMPT} -lt 15 ]] && exit 0

# --- Pass 1: relevant memory injection (unchanged behavior) ---------------

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

# --- Pass 2: notify on memories received from peer nodes since last prompt ---

$VENV - <<PYEOF
import sys, asyncio, os, json, datetime, pathlib
sys.path.insert(0, "$HOME/.local/share/mem-fusion")
import core

LAST_TS_FILE = """${LAST_TS_FILE}"""
QUEUE_DIR    = """${QUEUE}"""
MAX_NOTIFIED = 10
PER_ORIGIN_PREVIEW = 3
SNIPPET_LEN = 140

# Resolve self node_name from Constellation config (best-effort).
SELF_NODE = None
try:
    cfg_path = pathlib.Path.home() / ".local/share/mem-fusion/constellation/config.json"
    if cfg_path.exists():
        SELF_NODE = json.loads(cfg_path.read_text()).get("node_name")
except Exception:
    pass

async def main():
    os.makedirs(QUEUE_DIR, exist_ok=True)

    # Read last prompt timestamp for this session.
    try:
        with open(LAST_TS_FILE) as f:
            since_iso = f.read().strip()
    except FileNotFoundError:
        since_iso = None

    # Update timestamp now so concurrent prompts don't double-notify.
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(LAST_TS_FILE, "w") as f:
        f.write(now_iso)

    # First prompt of the session: no baseline yet, skip notification.
    if since_iso is None:
        return

    # Fetch recent memories (broad window; filter in Python).
    result = await core.search_recent({"hours": 24, "top_k": 100})
    candidates = result.get("results", [])

    received = []
    for m in candidates:
        # Arrival time: prefer received_at (set on /memory/put by Constellation
        # when a memory came from a peer); fall back to timestamp (local store
        # time, which on locally-created memories equals submitted_at).
        arrival = m.get("received_at") or m.get("timestamp")
        if not arrival or arrival <= since_iso:
            continue

        # Only memories from peer nodes — skip self-originated.
        origin = m.get("origin_node")
        if not origin:
            continue
        if SELF_NODE and origin == SELF_NODE:
            continue

        received.append(m)

    if not received:
        return

    # Cap and group by origin_node for readable summary.
    received = received[:MAX_NOTIFIED]
    by_origin = {}
    for m in received:
        by_origin.setdefault(m["origin_node"], []).append(m)

    lines = ["<received_memories>"]
    lines.append(
        f"Since your last prompt, {len(received)} memory(ies) arrived from peers:"
    )
    for origin in sorted(by_origin):
        mems = by_origin[origin]
        lines.append(f"  • {origin}: {len(mems)}")
        for m in mems[:PER_ORIGIN_PREVIEW]:
            snippet = m.get("content", "")[:SNIPPET_LEN].replace("\n", " ").strip()
            mtype = m.get("type", "memory")
            mid = (m.get("id") or "")[:8]
            lines.append(f"    - [{mtype}] {snippet}... (id: {mid})")
        if len(mems) > PER_ORIGIN_PREVIEW:
            lines.append(f"    ... and {len(mems) - PER_ORIGIN_PREVIEW} more")
    lines.append("</received_memories>")
    print("\n".join(lines))

asyncio.run(main())
PYEOF
