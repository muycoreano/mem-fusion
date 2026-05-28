# MEM_FUSION_FOR_DESKTOP.md — one-shot setup for the Claude desktop app

> **⚠️ Not yet fully tested.** Treat this as a preliminary procedure. The underlying integration is verified: mem-fusion's MCP server runs against the Claude (Cowork) desktop app as of 2026-05-21 with `memory_stats`, `search_memory`, `store_memory`, and `search_recent` all confirmed working when registered in `claude_desktop_config.json` and the local Qdrant + Ollama daemons are running. **What has NOT been end-to-end tested:** the exact `jq`-merge step on a real desktop config with pre-existing entries, the prerequisite-check pattern on a clean machine, and the speculative Step 6 / Step 7 sections. There is also a known warm-up quirk — MCP tools may not appear in the first message of a desktop session and only register from the second message onward. If a step fails or surprises you, surface the failure to the user instead of guessing, and consider filing a bug per `HOW-TO-CONTRIBUTE.md`.

**For another Claude** running in the Claude (Cowork) desktop app. Follow the steps in order. Each step is self-checking — if a verification fails, STOP and surface the failure to the user instead of guessing.

**Assumption:** mem-fusion is already installed for the Claude Code CLI on this machine (Qdrant + Ollama daemons running, `mem_fusion.py` in `~/.local/share/mem-fusion/`, MCP server registered with the CLI). If not, stop here and tell the user to run `install.sh` from <https://github.com/muycoreano/mem-fusion> first.

**Goal:** add mem-fusion as an MCP server in the desktop app's config, restart, verify. The desktop app and CLI share the same local Qdrant store, so memories written from either surface are visible to both.

---

## Step 1 — Verify the prerequisites

Run all four. If any fails: STOP, report the failure.

```bash
test -x "$HOME/.local/share/mem-fusion/venv/bin/python" || echo "MISSING: venv python"
test -f "$HOME/.local/share/mem-fusion/mem_fusion.py" || echo "MISSING: mem_fusion.py"
curl -sf http://127.0.0.1:6333/healthz >/dev/null || echo "DOWN: Qdrant"
curl -sf http://127.0.0.1:11434/api/tags >/dev/null || echo "DOWN: Ollama"
```

If all four are silent: prerequisites OK, continue.

---

## Step 2 — Locate the desktop config

macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

The file likely already exists with a `preferences` block. You'll add an `mcpServers` block alongside, never replacing the existing JSON.

If the file doesn't exist, create it with: `mkdir -p ~/Library/Application\ Support/Claude && echo '{}' > ~/Library/Application\ Support/Claude/claude_desktop_config.json`

---

## Step 3 — Add the mem-fusion MCP entry (safe merge with `jq`)

```bash
CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
jq '.mcpServers["mem-fusion"] = {
  command: "'"$HOME"'/.local/share/mem-fusion/venv/bin/python",
  args: ["'"$HOME"'/.local/share/mem-fusion/mem_fusion.py"]
}' "$CFG" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"
```

Verify the edit:

```bash
jq '.mcpServers["mem-fusion"]' "$CFG"
```

Expected output: a JSON object with `command` and `args` pointing at the venv python and `mem_fusion.py`.

---

## Step 4 — Restart the desktop app

Tell the user: full quit (Cmd-Q on macOS), then reopen. Just closing the window isn't enough — MCP servers are loaded at process start.

---

## Step 5 — Verify the tools are live

After the user reopens the app, in a new chat say:

> "Call `mcp__mem-fusion__memory_stats` and tell me the result."

Expected: a JSON-ish response with `total_memories`, `by_type`, `last_stored`. If the tool isn't found, the MCP didn't load — most common causes are:

| Symptom | Likely cause | Fix |
|---|---|---|
| Tool not listed in slash menu | App didn't restart fully | Full Cmd-Q, reopen |
| MCP startup error in logs | Path mismatch in config | Re-run Step 3, double-check `$HOME` |
| Tool calls hang | Qdrant or Ollama died | Re-run Step 1 verification |

---

## Step 6 — Smoke test (optional)

```
Claude in desktop: Use mem-fusion to store the memory: "Desktop app MCP
wired up on $(hostname) at $(date)". Type=context, importance=2,
project=install-test, tags=["desktop-smoke-test"].

Then use search_memory to find it again with the query "desktop MCP".
```

If both store + search round-trip cleanly: setup is done.

---

## What you get from the desktop app

- The full `mcp__mem-fusion__*` tool surface: `store_memory`, `search_memory`, `search_recent`, `memory_stats`, `upsert_memory`, `find_or_create`, `delete_memory`, `get_related`, `export_record`, `add_groups`, `add_connector_ids`, plus the v0.5 connector tools (`load_connectors_config`, `build_connector_envelope`, `ingest_connector_message`, `get_connector_cursor`, `set_connector_cursor`) and Constellation tools (`group_pull`, `group_push`) when Constellation is configured.
- Shared local Qdrant store with the CLI — memories you store from either surface are visible to both.

## What you DON'T get (CLI-only)

- The `/remember` slash command + its routing logic (it's a Claude Code skill in `~/.claude/skills/remember/`, not loaded by the desktop app).
- The session-start hook that pre-loads memory context.
- The UserPromptSubmit hook that injects semantically-relevant memories on each prompt.
- The Stop hook that ingests session transcripts.

In the desktop app, use natural language ("store this as a memory tagged X" / "search my memories for Y") and Claude will call the MCP tools directly. The orchestration that `/remember` provides — explicit group routing, connector dispatch, push-on-store — would need to happen via the desktop's own conversation logic rather than via skill files.

## (Speculative) — if the desktop app supports skill imports

If the desktop app exposes a way to import Claude Code skills (e.g., a settings panel that points at a skills directory), the SKILL files this machine has that are most useful from the desktop are:

| Skill | Path | What it does |
|---|---|---|
| `remember` | `~/.claude/skills/remember/SKILL.md` | Store/search/route memories with explicit group + connector dispatch |
| `mem-fusion-slack-connector` | `~/.claude/skills/mem-fusion-slack-connector/SKILL.md` | Set up + operate Slack connectors |

If you find an import mechanism, try `remember` first — it's the highest-leverage skill.

If skills aren't supported in the desktop app, the MCP tools alone cover ad-hoc store/search. The CLI is the right surface for full skill orchestration.

---

## Removing the entry

If you ever want to disable mem-fusion in the desktop app without touching the CLI:

```bash
jq 'del(.mcpServers["mem-fusion"])' "$CFG" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"
```

Then restart the app.

---

## What this does NOT modify

- Claude Code CLI config (`~/.claude.json`, `~/.claude/settings.json`)
- mem-fusion install at `~/.local/share/mem-fusion/`
- Qdrant data, Ollama models, launchd plists
- Constellation peer membership

Only the desktop app's MCP server list. The change is fully reversible by Step 7's removal command.
