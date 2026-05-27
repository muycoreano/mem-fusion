#!/usr/bin/env bash
# Fix 0.5.0-020 — SessionStart hook pre-loads relevant context instead
# of emitting a status header only.
#
# Bug (observed 2026-05-26 by Mitch on mc-macbookpro during a v0.5
# Cowork-desktop session):
#
# session_prime.sh emitted a `<memfusion_status>` block with stats +
# three 80-char snippets + the literal hint "Use search_memory(query=…)
# to retrieve relevant context.". The hint was easy to misread as
# "context is loaded" — meaning the assistant skipped the explicit
# search_memory call and proceeded with no project context until the
# first user prompt triggered the UserPromptSubmit hook's semantic
# pull. On short sessions this meant the SessionStart hook contributed
# zero loaded context.
#
# FIX SHAPE: session_prime.sh now derives a session-start query from
# (cwd basename + git branch + last 3 commit subjects), runs
#   search_memory(query, top_k=6, min_importance=3)
#   search_recent(hours=72, top_k=5)
# dedupes by id, and emits the SAME `<memory_context>` envelope that
# prompt_memory_inject.sh uses per user prompt. Capped at 3000 chars
# total to bound token cost. The `<memfusion_status>` banner is kept
# (lean, single-line) for liveness signal; the misleading "Use
# search_memory" hint is replaced with explicit guidance that context
# is pre-loaded and search_memory should only be called for topics not
# covered above.
#
# DEPLOYS:
#   - Patched ~/.local/share/mem-fusion/scripts/session_prime.sh
#     from this repo's src/scripts/session_prime.sh.
#
# BACK-COMPAT: hook signature unchanged (stdin JSON in, stdout block
# out, exit 0). Hooks that called the old script keep working; only
# stdout content shape changes (now richer, still in the same status
# + memory_context structure the assistant already parses).
#
# OUT OF SCOPE: this script does NOT modify the Claude Code hook
# timeout in ~/.claude/settings.json. On rare Ollama cold-start runs
# the new script may take 4-6s vs. the original 0.5-1s; if the
# default `timeout: 8` is hit in practice, bump it to 12 in
# settings.json manually — that file is per-user, not a mem-fusion
# install artifact.
#
# Idempotent. Marker: presence of the "Session-start context pre-loaded"
# phrase in the deployed session_prime.sh. Re-runs are full no-ops.

set -euo pipefail

MEMFUSION="${HOME}/.local/share/mem-fusion"
DEPLOYED="${MEMFUSION}/scripts/session_prime.sh"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SRC_HOOK="${SRC_ROOT}/scripts/session_prime.sh"

MARKER="Session-start context pre-loaded"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-020: SessionStart hook pre-loads relevant context"

[[ -d "$MEMFUSION" ]] || { log "ERROR: $MEMFUSION not found — is mem-fusion installed?"; exit 1; }
[[ -f "$SRC_HOOK" ]]  || { log "ERROR: $SRC_HOOK not found (repo layout drift?)"; exit 1; }

if [[ -f "$DEPLOYED" ]] && grep -q "$MARKER" "$DEPLOYED"; then
    log "Already applied: '$MARKER' present in $DEPLOYED. No-op."
    exit 0
fi

log "Deploying new session_prime.sh from $SRC_HOOK"
mkdir -p "$(dirname "$DEPLOYED")"
cp "$SRC_HOOK" "$DEPLOYED"
chmod +x "$DEPLOYED"

if grep -q "$MARKER" "$DEPLOYED"; then
    log "OK: marker '$MARKER' present in deployed hook."
else
    log "ERROR: deployed hook does not contain the expected marker."
    exit 1
fi

log "Done. The next Claude Code session will pre-load relevant memories at SessionStart."
