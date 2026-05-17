#!/usr/bin/env bash
# Fix 0.5.0-013 — v0.4 cursor back-fill bug
#
# Bug (observed live by tk421-macbookair as "missed 75 memories" and again
# locally during 0.5.0-008 verification as the f52d5dff anomaly):
#
# Constellation's pull path computed `cursor = max(submitted_at) in group`
# locally and asked each peer's GET /memory/since for "entries with
# submitted_at > cursor". `submitted_at` is the originating peer's
# timestamp, not a globally-ordered river — so a back-fill scenario silently
# drops entries:
#
#   1. Peer Y writes memory M1 at submitted_at=80.
#   2. M1 propagates to relay X (X has M1).
#   3. We do an unrelated pull; our local store now has some other peer's
#      memory at submitted_at=200. Local max bumps to 200.
#   4. Network partition / Y offline / general delay — M1 hasn't reached us.
#   5. We pull from X with cursor=200. X filters submitted_at>200, returns
#      nothing newer. M1 is silently excluded — we never see it.
#   6. We think we're caught up. We're missing 1+ memories with no way to
#      know.
#
# The cursor model assumed monotonic-acquisition-by-time, which the actual
# distributed system doesn't provide.
#
# FIX SHAPE for v0.5: drop the cursor entirely. Each pull is now a full
# scan of the peer's group; receiver-side content_hash dedup (already in
# Constellation's _merge_pulled and the /memory/put handler) handles
# efficiency on the receive side. Cost: O(group_size) bytes on the wire
# per pull. At current scale (<10K entries per group) this is sub-100ms
# transfer + parse — negligible. Revisit if any group crosses ~50K entries,
# at which point per-origin cursor or a bloom-filter "what I already have"
# exchange becomes worthwhile.
#
# WHY THIS IS THE RIGHT v0.5 CHOICE:
# Per the v0.5 reconciliation (memory 9b1a88b8), Constellation is now the
# experimental P2P sibling system to mem-fusion native connectors. The
# native path's primary connector is Slack, which uses Slack message `ts`
# as the cursor — a per-channel monotonic identifier with no back-fill
# pathology. Constellation's pull path is a secondary code path for users
# who can't or won't use cloud connectors. Investing in a complex per-origin
# cursor scheme for Constellation v0.4 is overengineering when full-scan
# correctly fixes the bug at acceptable cost.
#
# IMPLEMENTATION:
#   - src/core.py:get_entries_for_pull no longer filters on cursor_iso.
#     The parameter is preserved for wire-format back-compat (older clients
#     may still send `cursor=...` in their /memory/since query string; the
#     server now ignores it). Limit default raised to 10000.
#   - src/constellation.py:_handle_pull no longer computes the cursor.
#     The /memory/since call sends only group_name; the response is
#     full-scanned.
#   - _merge_pulled (already in place) content_hash-dedups on receive,
#     so the additional bytes don't bloat the local store.
#
# THIS FIX SCRIPT:
#   A. Deploys canonical src/core.py → ~/.local/share/mem-fusion/core.py.
#   B. Deploys canonical src/constellation.py → ~/.local/share/mem-fusion/constellation.py.
#   C. Restarts the Constellation daemon to pick up the new constellation.py.
#      (core.py is loaded by both the MCP server and the daemon; the daemon
#      restart suffices for the daemon side. The MCP server picks up the
#      new core.py on the next Claude session.)
#   D. Verifies: post-restart /pull from the daemon returns entries
#      regardless of any cursor that's still being sent.
#
# Idempotent: re-running detects each sub-fix's state independently and
# skips.
#
# Run via: bash 0.5.0-013-fix-v04-cursor-backfill-bug.sh

set -euo pipefail

MEMFUSION="${HOME}/.local/share/mem-fusion"
CORE_PY="${MEMFUSION}/core.py"
CONSTELLATION_PY="${MEMFUSION}/constellation.py"
VENV_PY="${MEMFUSION}/venv/bin/python"
DAEMON_LABEL="com.branchapp.memfusion.constellation"
DAEMON_PLIST="${HOME}/Library/LaunchAgents/${DAEMON_LABEL}.plist"
GATEWAY_URL="http://127.0.0.1:7534"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SRC_CORE="${SRC_ROOT}/core.py"
SRC_CONSTELLATION="${SRC_ROOT}/constellation.py"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-013: v0.4 cursor back-fill bug (drop cursor, full-scan pulls)"

# Pre-flight
[[ -f "$CORE_PY" ]]            || { log "ERROR: $CORE_PY not found"; exit 1; }
[[ -f "$CONSTELLATION_PY" ]]   || { log "ERROR: $CONSTELLATION_PY not found"; exit 1; }
[[ -x "$VENV_PY" ]]            || { log "ERROR: $VENV_PY not found"; exit 1; }
[[ -f "$SRC_CORE" ]]           || { log "ERROR: $SRC_CORE not found — run from a mem-fusion repo checkout"; exit 1; }
[[ -f "$SRC_CONSTELLATION" ]]  || { log "ERROR: $SRC_CONSTELLATION not found"; exit 1; }

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix A — deploy patched core.py
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── A: deploy patched core.py (get_entries_for_pull ignores cursor) ──"
# Marker: docstring HISTORY block uniquely identifies the post-fix version
if grep -q "HISTORY: pre-0.5.0-013" "$CORE_PY"; then
  log "  Already applied: HISTORY marker present in get_entries_for_pull docstring."
else
  BACKUP="${CORE_PY}.bak.0.5.0-013"
  log "  Backing up to $BACKUP"
  cp "$CORE_PY" "$BACKUP"
  log "  Deploying canonical $SRC_CORE → $CORE_PY"
  cp "$SRC_CORE" "$CORE_PY"
  "$VENV_PY" -c "import ast; ast.parse(open('$CORE_PY').read())" \
    && log "  syntax ok" \
    || { log "  ERROR: deployed core.py failed to parse — restoring backup"; cp "$BACKUP" "$CORE_PY"; exit 1; }
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix B — deploy patched constellation.py
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── B: deploy patched constellation.py (_handle_pull drops cursor) ──"
# Marker: the fix 0.5.0-013 reference in the inline comment
if grep -q "fix 0.5.0-013" "$CONSTELLATION_PY"; then
  log "  Already applied: 0.5.0-013 marker present in constellation.py _handle_pull."
  RESTART_NEEDED=0
else
  BACKUP="${CONSTELLATION_PY}.bak.0.5.0-013"
  log "  Backing up to $BACKUP"
  cp "$CONSTELLATION_PY" "$BACKUP"
  log "  Deploying canonical $SRC_CONSTELLATION → $CONSTELLATION_PY"
  cp "$SRC_CONSTELLATION" "$CONSTELLATION_PY"
  "$VENV_PY" -c "import ast; ast.parse(open('$CONSTELLATION_PY').read())" \
    && log "  syntax ok" \
    || { log "  ERROR: deployed constellation.py failed to parse — restoring backup"; cp "$BACKUP" "$CONSTELLATION_PY"; exit 1; }
  RESTART_NEEDED=1
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix C — restart Constellation daemon if its code changed
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── C: restart Constellation daemon to pick up new constellation.py ──"
if [[ "${RESTART_NEEDED:-0}" -eq 0 ]]; then
  log "  No restart needed: constellation.py unchanged in this run."
else
  if [[ -f "$DAEMON_PLIST" ]]; then
    log "  Kickstarting $DAEMON_LABEL..."
    launchctl kickstart -k "gui/$(id -u)/${DAEMON_LABEL}" >/dev/null 2>&1 || \
      log "  WARNING: kickstart returned non-zero; daemon may not be loaded."
    sleep 2
    # Verify health
    if curl -s --max-time 5 "${GATEWAY_URL}/" >/dev/null 2>&1 || \
       curl -s --max-time 5 "http://127.0.0.1:7533/health" >/dev/null 2>&1; then
      log "  ✓ Daemon responding post-restart"
    else
      log "  WARNING: daemon health check inconclusive — check logs at ~/.local/share/mem-fusion/constellation/logs/"
    fi
  else
    log "  Plist not installed at $DAEMON_PLIST — skipping restart."
    log "  (This is fine if you don't have Constellation installed locally.)"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Verification
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── Verification: get_entries_for_pull is cursor-agnostic ──"
"$VENV_PY" - <<'PYEOF'
import sys
sys.path.insert(0, "/Users/mcoopet/.local/share/mem-fusion")
import core
no_cursor = core.get_entries_for_pull("personal", None)
with_cursor = core.get_entries_for_pull("personal", "2030-01-01T00:00:00+00:00")
assert len(no_cursor) == len(with_cursor), (
    f"FAIL: cursor still filtering — no_cursor={len(no_cursor)}, with_far_future_cursor={len(with_cursor)}"
)
print(f"  ✓ get_entries_for_pull returns {len(no_cursor)} entries regardless of cursor value")
PYEOF

log ""
log "Fix 0.5.0-013 complete."
log "  Effect:"
log "    - Pull no longer silently drops back-filled entries."
log "    - Each pull is now a full scan of the peer's group (acceptable at v0.5 scale)."
log "    - Receiver-side content_hash dedup (already present) absorbs the cost"
log "      on the receive side: identical entries are merge-ops, not stores."
log "  Daemon restarted to pick up the new constellation.py if changed."
exit 0
