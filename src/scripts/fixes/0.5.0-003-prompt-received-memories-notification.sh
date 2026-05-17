#!/usr/bin/env bash
# Fix 0.5.0-003 — Patch UserPromptSubmit hook to notify on received peer memories.
#
# Adds a second pass to prompt_memory_inject.sh that surfaces a summary of
# memories received from peer nodes (origin_node != self) since the last
# user prompt in the current Claude session. Output is a <received_memories>
# context block that Claude can naturally raise to the user.
#
# Mechanism:
# - Pass 1 (unchanged): inject semantically-relevant memories above 0.75 score.
# - Pass 2 (new): track last-prompt timestamp per session in
#   queue/last-prompt-ts-${CLAUDE_SESSION_ID}.txt. On each prompt, query
#   recent memories, filter to those with received_at > last_prompt_ts AND
#   origin_node != self, group by origin, print summary.
#
# Idempotent: if the deployed script already has the Pass 2 marker, skip
# the install and exit 0.
#
# Run via:
#   bash <path-to>/0.5.0-003-prompt-received-memories-notification.sh

set -euo pipefail

DEPLOYED="${HOME}/.local/share/mem-fusion/scripts/prompt_memory_inject.sh"
SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/prompt_memory_inject.sh"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-003: prompt_memory_inject Pass 2 (received peer memories)"

# Idempotency check — Pass 2 introduces the "received_memories" marker.
if [[ -f "$DEPLOYED" ]] && grep -q "received_memories" "$DEPLOYED"; then
  log "Already patched: $DEPLOYED contains the Pass 2 marker."
  exit 0
fi

if [[ ! -f "$DEPLOYED" ]]; then
  log "ERROR: $DEPLOYED not found. Mem-fusion may not be installed correctly."
  exit 1
fi

if [[ ! -f "$SRC" ]]; then
  log "ERROR: source script not found at $SRC."
  log "       Run this from the cloned mem-fusion repo so the canonical"
  log "       prompt_memory_inject.sh is accessible."
  exit 1
fi

log "Backing up current deployed script to ${DEPLOYED}.bak-${TIMESTAMP}"
cp "$DEPLOYED" "${DEPLOYED}.bak-${TIMESTAMP}"

log "Copying new prompt_memory_inject.sh from src to deployed location"
cp "$SRC" "$DEPLOYED"
chmod +x "$DEPLOYED"

log "Verifying executable + Pass 2 marker present"
if [[ ! -x "$DEPLOYED" ]]; then
  log "ERROR: deployed script is not executable after copy."
  exit 1
fi
if ! grep -q "received_memories" "$DEPLOYED"; then
  log "ERROR: Pass 2 marker missing in deployed script after copy."
  exit 1
fi

log "Patch applied successfully."
log "  - Pass 2 fires on every UserPromptSubmit beyond the first in a session."
log "  - First prompt of any session establishes the baseline timestamp;"
log "    subsequent prompts compare against it."
log "  - Per-session state lives in: ${HOME}/.local/share/mem-fusion/queue/"
log ""
log "Verify with: tail -F ${HOME}/.local/share/mem-fusion/logs/*.log"
log "(no daemon restart needed — hooks read the file fresh on each invocation)"

exit 0
