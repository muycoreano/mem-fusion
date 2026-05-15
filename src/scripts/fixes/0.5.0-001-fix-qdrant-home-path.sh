#!/usr/bin/env bash
# Fix 0.5.0-001 — qdrant-config.yaml '$HOME' literal expansion bug
#
# Bug: Qdrant does NOT expand environment variables in YAML config. Earlier
# qdrant-config.yaml used `storage_path: $HOME/.local/share/mem-fusion/qdrant-data`
# which Qdrant interpreted LITERALLY, creating a directory named '$HOME'
# inside the install location:
#
#   ~/.local/share/mem-fusion/$HOME/.local/share/mem-fusion/qdrant-data/  ← actual data
#   ~/.local/share/mem-fusion/qdrant-data/                                ← empty placeholder
#
# Functionally harmless — Qdrant happily writes to the literal '$HOME' path —
# but ugly and confusing if you ever try to back up the "real" location.
#
# This script:
#   1. Stops Qdrant via launchctl
#   2. Moves the misplaced data tree to the intended location
#   3. Cleans up the empty literal '$HOME' placeholder directory tree
#   4. Patches the deployed qdrant-config.yaml to use the literal absolute path
#   5. Restarts Qdrant and verifies health
#
# Idempotent: re-running after the fix is applied detects clean state and exits 0.
#
# Run via:
#   bash <path-to>/0.5.0-001-fix-qdrant-home-path.sh

set -euo pipefail

MEMFUSION="${HOME}/.local/share/mem-fusion"
MISPLACED="${MEMFUSION}/\$HOME/.local/share/mem-fusion/qdrant-data"
INTENDED="${MEMFUSION}/qdrant-data"
CONFIG="${MEMFUSION}/qdrant-config.yaml"
PLIST="${HOME}/Library/LaunchAgents/com.branchapp.memfusion.qdrant.plist"
LITERAL_HOME_ROOT="${MEMFUSION}/\$HOME"
LAUNCHD_LABEL="com.branchapp.memfusion.qdrant"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-001: qdrant-config.yaml \$HOME literal expansion bug"

# Idempotency check — if no misplaced data AND config has no '$HOME' anywhere, bail clean.
config_has_home=0
if [[ -f "$CONFIG" ]] && grep -q '\$HOME' "$CONFIG"; then
  config_has_home=1
fi
if [[ ! -d "$MISPLACED" ]] && [[ "$config_has_home" -eq 0 ]]; then
  log "Already fixed: no misplaced data, no \$HOME in $CONFIG."
  exit 0
fi

# 1. Stop Qdrant if running.
if launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"; then
  log "Stopping Qdrant ($LAUNCHD_LABEL)"
  launchctl unload "$PLIST"
  sleep 1
else
  log "Qdrant not running (or no plist for $LAUNCHD_LABEL) — proceeding"
fi

# 2. Migrate data tree if misplaced data exists.
if [[ -d "$MISPLACED" ]]; then
  # Refuse to migrate if both locations have data — needs human review.
  if [[ -d "$INTENDED" ]] && [[ -n "$(ls -A "$INTENDED" 2>/dev/null)" ]]; then
    log "ERROR: both misplaced and intended directories have data."
    log "  misplaced: $MISPLACED"
    log "  intended:  $INTENDED"
    log "Manual review required. Reloading Qdrant and exiting."
    launchctl load "$PLIST" 2>/dev/null || true
    exit 1
  fi
  # Remove empty intended dir if present so mv lands cleanly.
  if [[ -d "$INTENDED" ]]; then
    log "Removing empty intended dir at $INTENDED"
    rmdir "$INTENDED"
  fi
  log "Moving misplaced data: $MISPLACED -> $INTENDED"
  mv "$MISPLACED" "$INTENDED"
  log "Cleaning up empty literal-\$HOME placeholder tree at $LITERAL_HOME_ROOT"
  rm -rf "$LITERAL_HOME_ROOT"
else
  log "No misplaced data to migrate"
fi

# 3. Patch deployed qdrant-config.yaml — replace literal '$HOME' with the absolute home path.
if [[ -f "$CONFIG" ]] && grep -q '\$HOME' "$CONFIG"; then
  log "Patching $CONFIG (replacing literal \$HOME with $HOME)"
  # Use '|' as sed delimiter to avoid conflicts with '/' inside $HOME.
  sed -i.bak "s|\$HOME|${HOME}|g" "$CONFIG"
  log "Backup written to ${CONFIG}.bak"
elif [[ ! -f "$CONFIG" ]]; then
  log "WARNING: $CONFIG not found — skipping config patch"
else
  log "Config already clean (no \$HOME found in $CONFIG)"
fi

# 4. Restart Qdrant.
log "Starting Qdrant"
launchctl load "$PLIST"

# 5. Verify Qdrant comes back up healthy.
log "Waiting for Qdrant to respond on http://127.0.0.1:6333/healthz..."
if curl -s --retry 12 --retry-delay 1 --retry-connrefused http://127.0.0.1:6333/healthz >/dev/null; then
  log "OK Qdrant healthy at http://127.0.0.1:6333"
  log "Data location: $INTENDED"
  log "Fix 0.5.0-001 complete."
  exit 0
else
  log "ERROR: Qdrant didn't come up — check ${MEMFUSION}/logs/qdrant-error.log"
  exit 1
fi
