#!/usr/bin/env python3
"""
Remove cowork-memory hook entries from ~/.claude/settings.json.

Run this as part of the cowork-memory → mem-fusion v0.4 upgrade, before
wire_hooks.py installs the v0.4 hook entries. After both scripts run, only
the mem-fusion (and any unrelated) hook entries remain — no double-fire.

Idempotent: re-runs are no-ops once cowork-memory entries are already gone.
"""
import json
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude/settings.json"
COWORK_PATH_FRAGMENT = "/.local/share/cowork-memory/"

if not SETTINGS_PATH.exists():
    print(f"No {SETTINGS_PATH} — nothing to unwire")
    raise SystemExit(0)

settings = json.loads(SETTINGS_PATH.read_text())
hooks = settings.get("hooks", {})

removed = 0
for event in list(hooks.keys()):
    new_blocks = []
    for block in hooks[event]:
        kept_hooks = []
        for h in block.get("hooks", []):
            if COWORK_PATH_FRAGMENT in h.get("command", ""):
                removed += 1
            else:
                kept_hooks.append(h)
        if kept_hooks:
            new_blocks.append({**block, "hooks": kept_hooks})
    if new_blocks:
        hooks[event] = new_blocks
    else:
        del hooks[event]

settings["hooks"] = hooks
SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
print(f"Removed {removed} cowork-memory hook entries from {SETTINGS_PATH}")
