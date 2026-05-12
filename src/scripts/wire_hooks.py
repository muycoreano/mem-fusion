#!/usr/bin/env python3
import json, os
from pathlib import Path

settings_path = Path.home() / ".claude/settings.json"
home          = str(Path.home())

if settings_path.exists():
    settings = json.loads(settings_path.read_text())
else:
    settings = {}

new_hooks = {
    "SessionStart": [{
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": f"{home}/.local/share/mem-fusion/scripts/session_prime.sh",
            "timeout": 8,
        }],
    }],
    "UserPromptSubmit": [{
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": f"{home}/.local/share/mem-fusion/scripts/prompt_memory_inject.sh",
            "timeout": 2,
        }],
    }],
    "Stop": [{
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": (
                f"nohup {home}/.local/share/mem-fusion/venv/bin/python "
                f"{home}/.local/share/mem-fusion/scripts/ingest_session.py "
                f">> {home}/.local/share/mem-fusion/logs/ingest.log 2>&1 & "
                f"rm -f {home}/.local/share/mem-fusion/queue/seen-${{CLAUDE_SESSION_ID}}.txt"
            ),
            "timeout": 3,
        }],
    }],
    "PostToolUse": [{
        "matcher": "Write",
        "hooks": [{
            "type": "command",
            "command": f"{home}/.local/share/mem-fusion/scripts/capture_file_write.sh",
            "timeout": 2,
        }],
    }],
}

existing_hooks = settings.get("hooks", {})
for event, blocks in new_hooks.items():
    existing_blocks = existing_hooks.get(event, [])
    for new_block in blocks:
        is_dupe = any(
            eb.get("matcher") == new_block["matcher"]
            and any(h.get("command") == new_block["hooks"][0]["command"]
                    for h in eb.get("hooks", []))
            for eb in existing_blocks
        )
        if not is_dupe:
            existing_blocks.append(new_block)
    existing_hooks[event] = existing_blocks
settings["hooks"] = existing_hooks

settings_path.write_text(json.dumps(settings, indent=2))
print(f"Hooks merged into {settings_path}")
