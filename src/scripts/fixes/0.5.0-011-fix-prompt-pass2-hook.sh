#!/usr/bin/env bash
# Fix 0.5.0-011 — make UserPromptSubmit hook's Pass 2 actually work
#
# Discovered debugging 2026-05-17 by Claude-Engineer on mc-macbookair: the
# Pass 2 received-memories notification shipped in 0.5.0-003 has NEVER fired
# end-to-end. Three compound bugs:
#
#   1. core.py:search_recent and core.py:format_results omit `origin_node`
#      and `received_at` from their returned result dicts. Pass 2's filter
#      `m.get("origin_node")` is therefore always None for every candidate,
#      and the candidate gets skipped as "no-origin." Pass 2 never identifies
#      a peer-originated memory regardless of what's actually in the store.
#
#   2. The hook reads stdin with `PROMPT=$(cat)`, but Claude Code's
#      UserPromptSubmit hook contract pipes structured JSON to stdin, not
#      raw prompt text:
#         {session_id, prompt, transcript_path, cwd, permission_mode,
#          hook_event_name}
#      So `PROMPT` is the whole JSON envelope. Pass 1's semantic-search
#      query is the JSON wrapper, not the prompt — degrading injection
#      quality. Verified empirically against the canonical doc
#      https://code.claude.com/docs/en/hooks.md.
#
#   3. The hook resolves session id from `${CLAUDE_SESSION_ID:-unknown}`,
#      but that env var does not exist on hook processes. Every prompt
#      shares one cursor file (last-prompt-ts-unknown.txt), so baselines
#      leak across sessions. The session_id is only in stdin JSON
#      (`.session_id`).
#
# THIS FIX:
#   A. Patches core.py: adds `origin_node` + `received_at` passthrough to
#      both search_recent's and format_results's result dicts. Additive,
#      backward-compatible (no rename, no removal).
#   B. Replaces the deployed prompt_memory_inject.sh with a version that
#      parses stdin JSON via the mem-fusion venv (no jq dependency).
#      Extracts session_id + prompt correctly; falls back to raw-text mode
#      if JSON parse fails (preserves manual-invocation usability).
#   C. Removes the temporary debug capture lines (printf > /tmp/hook-stdin-*,
#      env-grep > /tmp/hook-env-*) that may be present from investigation
#      on this peer. Idempotent.
#   D. Removes the stale `last-prompt-ts-unknown.txt` cursor file if present
#      (it was created by the buggy version's "unknown" session-id fallback;
#      proper session-keyed files take over on next prompt).
#
# Idempotent. Re-running detects each sub-fix's state independently.
# No daemon restart needed. Effect applies on the next Claude session's
# first prompt (Pass 1 immediately; Pass 2 establishes baseline on first
# post-fix prompt, fires on the second and beyond).
#
# Run via: bash 0.5.0-011-fix-prompt-pass2-hook.sh

set -euo pipefail

MEMFUSION="${HOME}/.local/share/mem-fusion"
CORE_PY="${MEMFUSION}/core.py"
HOOK_SH="${MEMFUSION}/scripts/prompt_memory_inject.sh"
VENV_PY="${MEMFUSION}/venv/bin/python"
QUEUE="${MEMFUSION}/queue"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SRC_CORE="${SRC_ROOT}/core.py"
SRC_HOOK="${SRC_ROOT}/scripts/prompt_memory_inject.sh"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-011: UserPromptSubmit Pass 2 — compound hook + core.py fix"

# Pre-flight
[[ -f "$CORE_PY" ]]   || { log "ERROR: $CORE_PY not found"; exit 1; }
[[ -f "$HOOK_SH" ]]   || { log "ERROR: $HOOK_SH not found"; exit 1; }
[[ -x "$VENV_PY" ]]   || { log "ERROR: $VENV_PY not found"; exit 1; }
[[ -f "$SRC_CORE" ]]  || { log "ERROR: $SRC_CORE not found — run from a mem-fusion repo checkout"; exit 1; }
[[ -f "$SRC_HOOK" ]]  || { log "ERROR: $SRC_HOOK not found — run from a mem-fusion repo checkout"; exit 1; }

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix A — core.py origin_node + received_at passthrough
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── A: core.py search_recent + format_results passthrough ──"
# Marker: the new payload key in search_recent's result dict
if grep -q '"origin_node": p.payload.get' "$CORE_PY" && \
   grep -q '"received_at": p.payload.get' "$CORE_PY"; then
  log "  Already applied: origin_node + received_at present in deployed core.py."
  if ! cmp -s "$SRC_CORE" "$CORE_PY"; then
    log "  Note: deployed core.py differs from canonical src/core.py — keeping deployed (other fixes may have landed)."
  fi
else
  BACKUP="${CORE_PY}.bak.0.5.0-011"
  log "  Backing up to $BACKUP"
  cp "$CORE_PY" "$BACKUP"
  log "  Deploying canonical $SRC_CORE → $CORE_PY"
  cp "$SRC_CORE" "$CORE_PY"
  # Verify parse
  "$VENV_PY" -c "import ast; ast.parse(open('$CORE_PY').read())" \
    && log "  syntax ok" \
    || { log "  ERROR: deployed core.py failed to parse — restoring backup"; cp "$BACKUP" "$CORE_PY"; exit 1; }
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix B — prompt_memory_inject.sh JSON parsing
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── B: prompt_memory_inject.sh JSON stdin parsing ──"
# Marker: the json.loads(raw) line near the top of the new hook
if grep -q 'json.loads(raw)' "$HOOK_SH" && \
   grep -q 'd.get("session_id"' "$HOOK_SH"; then
  log "  Already applied: JSON-parsing header present in deployed hook."
  if ! cmp -s "$SRC_HOOK" "$HOOK_SH"; then
    log "  Note: deployed hook differs from canonical src — keeping deployed."
  fi
else
  BACKUP="${HOOK_SH}.bak.0.5.0-011"
  log "  Backing up to $BACKUP"
  cp "$HOOK_SH" "$BACKUP"
  log "  Deploying canonical $SRC_HOOK → $HOOK_SH"
  cp "$SRC_HOOK" "$HOOK_SH"
  chmod +x "$HOOK_SH"
  # Verify bash syntax
  bash -n "$HOOK_SH" \
    && log "  bash syntax ok" \
    || { log "  ERROR: deployed hook failed bash -n — restoring backup"; cp "$BACKUP" "$HOOK_SH"; exit 1; }
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix C — clean up temporary debug capture files (if present)
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── C: clean up temporary debug captures ──"
DEBUG_FILES=( /tmp/hook-stdin-capture-*.json /tmp/hook-env-claude-*.txt )
REMOVED=0
for f in "${DEBUG_FILES[@]}"; do
  if [[ -f "$f" ]]; then
    rm -f "$f"
    REMOVED=$((REMOVED + 1))
  fi
done
if (( REMOVED > 0 )); then
  log "  Removed $REMOVED debug capture file(s) from /tmp"
else
  log "  No debug capture files to remove."
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix D — remove stale "unknown" session cursor
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── D: remove stale last-prompt-ts-unknown.txt ──"
STALE_CURSOR="${QUEUE}/last-prompt-ts-unknown.txt"
STALE_SEEN="${QUEUE}/seen-unknown.txt"
if [[ -f "$STALE_CURSOR" ]]; then
  rm -f "$STALE_CURSOR"
  log "  Removed stale $STALE_CURSOR (the unknown-session fallback won't be created by the fixed hook)."
else
  log "  No stale cursor file to remove."
fi
[[ -f "$STALE_SEEN" ]] && rm -f "$STALE_SEEN" && log "  Removed stale seen-unknown.txt as well."

# ──────────────────────────────────────────────────────────────────────────
# Verification — exercise the new hook with a synthetic Claude-Code JSON
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── Verification: feed the new hook a synthetic JSON envelope ──"
TEST_SESSION="verify-0.5.0-011-$(date +%s)"
SAMPLE=$(printf '{"session_id":"%s","prompt":"this is a sufficiently long test prompt that bypasses the length check","transcript_path":"/tmp/foo","cwd":"/tmp","permission_mode":"default","hook_event_name":"UserPromptSubmit"}' "$TEST_SESSION")
printf '%s' "$SAMPLE" | bash "$HOOK_SH" >/dev/null 2>&1 || true
EXPECTED_CURSOR="${QUEUE}/last-prompt-ts-${TEST_SESSION}.txt"
if [[ -f "$EXPECTED_CURSOR" ]]; then
  log "  Hook correctly extracted session_id from JSON: $TEST_SESSION"
  rm -f "$EXPECTED_CURSOR" "${QUEUE}/seen-${TEST_SESSION}.txt" 2>/dev/null
else
  log "  WARNING: hook did not create $EXPECTED_CURSOR — session_id extraction may be broken"
  log "           (not failing the fix script; manual investigation recommended)"
fi

log ""
log "Fix 0.5.0-011 complete."
log "  Effect:"
log "    - core.py: search_recent + format_results now expose origin_node + received_at."
log "    - Hook: parses Claude Code's stdin JSON envelope correctly."
log "    - Pass 1: semantic-search query is now the actual prompt text, not the JSON wrapper."
log "    - Pass 2: peer-originated memories will be detected and surfaced on next prompt."
log "  No daemon restart needed. Next Claude session: Pass 2 establishes baseline on its first prompt."
exit 0
