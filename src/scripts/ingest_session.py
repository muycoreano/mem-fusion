#!/usr/bin/env python3
"""Session ingestion — Stop hook. Backgrounded; doesn't block exit."""
import json, logging, os, re, sys, hashlib
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR   = Path.home() / ".local/share/mem-fusion"
LOG_PATH     = MEMORY_DIR / "logs/ingest.log"
SESSIONS_DIR = Path.home() / ".claude/sessions"
DEDUP_FILE   = MEMORY_DIR / "queue/ingested_sessions.txt"
QUEUE_DIR    = MEMORY_DIR / "queue"

logging.basicConfig(filename=str(LOG_PATH), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest-session")


def load_dedup_registry():
    if DEDUP_FILE.exists():
        return set(DEDUP_FILE.read_text().splitlines())
    return set()


def mark_ingested(session_id):
    with open(DEDUP_FILE, "a") as f:
        f.write(session_id + "\n")


def find_latest_session():
    if not SESSIONS_DIR.exists():
        return None, []
    files = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None, []
    latest = files[0]
    sid    = latest.stem
    msgs   = []
    try:
        for line in latest.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except Exception as e:
        log.error("Failed to read session file: %s", e)
    return sid, msgs


def assess_session_quality(messages):
    if not messages:
        return {"skip": True, "reason": "empty"}
    user_turns = sum(1 for m in messages if m.get("role") == "human")
    asst_turns = sum(1 for m in messages if m.get("role") == "assistant")
    tool_calls = sum(1 for m in messages if m.get("role") == "assistant"
                     and any(isinstance(c, dict) and c.get("type") == "tool_use"
                             for c in (m.get("content") if isinstance(m.get("content"), list) else [])))
    write_edit = sum(1 for m in messages if m.get("role") == "assistant"
                     and any(isinstance(c, dict) and c.get("type") == "tool_use"
                             and c.get("name") in ("Write", "Edit", "write", "edit")
                             for c in (m.get("content") if isinstance(m.get("content"), list) else [])))
    if user_turns < 4:
        return {"skip": True, "reason": f"only {user_turns} user turns (< 4)"}
    if write_edit >= 5:    importance = 4
    elif write_edit >= 2 or tool_calls >= 5: importance = 3
    else:                  importance = 2
    return {"skip": False, "importance": importance, "tool_calls": tool_calls,
            "write_edit_calls": write_edit, "user_turns": user_turns}


def extract_text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    for sub in (block.get("content") or []):
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append(sub.get("text", "")[:200])
        return " ".join(parts)
    return ""


DECISION_PATTERNS = [
    r"we (?:decided|chose|picked|selected|went with|are going with|will use)\b.{10,120}",
    r"(?:decided|choosing|selecting) to\b.{10,120}",
    r"going with\b.{10,100}",
]
ERROR_RESOLUTION_PATTERNS = [
    r"(?:fixed|resolved|solved|the fix (?:is|was))\b.{10,150}",
    r"(?:the issue was|root cause)\b.{10,150}",
]
PREFERENCE_PATTERNS = [
    r"(?:always|never|don't|do not|please|prefer)\b.{10,100}",
    r"(?:i want|i'd like|i prefer)\b.{10,100}",
    r"next time\b.{10,100}",
]


def extract_signals(messages, session_id):
    memories = []
    full_text = []
    for msg in messages:
        role    = msg.get("role", "")
        content = extract_text_content(msg.get("content", ""))
        if not content:
            continue
        full_text.append(f"[{role}]: {content[:500]}")
        if role == "human":
            for pat in PREFERENCE_PATTERNS:
                for m in re.finditer(pat, content, re.IGNORECASE):
                    s = m.group(0).strip()[:200]
                    if len(s) > 20:
                        memories.append({"content": s, "type": "preference",
                                         "source": "hook", "session_id": session_id})
        elif role == "assistant":
            for pat in DECISION_PATTERNS:
                for m in re.finditer(pat, content, re.IGNORECASE):
                    s = m.group(0).strip()[:250]
                    if len(s) > 25:
                        memories.append({"content": s, "type": "decision",
                                         "source": "hook", "session_id": session_id})
            for pat in ERROR_RESOLUTION_PATTERNS:
                for m in re.finditer(pat, content, re.IGNORECASE):
                    s = m.group(0).strip()[:250]
                    if len(s) > 25:
                        memories.append({"content": s, "type": "error",
                                         "source": "hook", "session_id": session_id})

    seen = set()
    unique = []
    for mem in memories:
        h = hashlib.sha256(mem["content"].strip().lower().encode()).hexdigest()[:16]
        if h not in seen:
            seen.add(h)
            unique.append(mem)

    if full_text:
        unique.insert(0, {"content": f"[Session {session_id[:8]}] {chr(10).join(full_text[:6])[:600]}",
                          "type": "session", "source": "hook", "session_id": session_id})
    return unique


def store_via_api(memories, importance, project):
    import asyncio, urllib.request
    sys.path.insert(0, str(MEMORY_DIR))
    try:
        urllib.request.urlopen("http://127.0.0.1:6333/healthz", timeout=3)
    except Exception:
        log.error("Qdrant not reachable — queueing memories")
        for mem in memories:
            mem["importance"] = importance
            mem["project"]    = project
            ts   = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            h    = hashlib.sha256(mem["content"].encode()).hexdigest()[:8]
            (QUEUE_DIR / f"{ts}-{h}.json").write_text(json.dumps(mem))
        return

    # Hook-captured memories are always personal — they record this peer's
    # work and should never propagate to teammates without explicit user intent.
    import core
    stored = 0
    for mem in memories:
        try:
            r = asyncio.run(core.store_memory({
                **mem, "importance": importance, "project": project,
                "groups": ["personal"],
            }))
            if r.get("status") in ("stored", "merged"):
                stored += 1
        except Exception as e:
            log.error("Store failed: %s — %s", mem["content"][:40], e)
    log.info("Ingestion complete: %d/%d stored", stored, len(memories))


def main():
    log.info("=== Session ingestion started ===")
    project = os.getenv("CLAUDE_PROJECT", "")
    if not project:
        cwd = os.getcwd()
        project = Path(cwd).name if cwd != str(Path.home()) else "general"

    sid, messages = find_latest_session()
    if not sid:
        return

    if sid in load_dedup_registry():
        return

    q = assess_session_quality(messages)
    if q.get("skip"):
        mark_ingested(sid)
        return

    memories = extract_signals(messages, sid)
    if memories:
        store_via_api(memories, importance=q["importance"], project=project)
    mark_ingested(sid)
    log.info("=== Session ingestion complete ===")


if __name__ == "__main__":
    main()
