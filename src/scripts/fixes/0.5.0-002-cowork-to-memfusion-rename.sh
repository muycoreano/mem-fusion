#!/usr/bin/env bash
# Fix 0.5.0-002 — Complete cowork-memory → mem-fusion infrastructure rename
#
# Migrates a peer from the hybrid state (legacy com.cowork.* launchd labels
# + cowork-memory install dir running alongside the newer mem-fusion code)
# to canonical mem-fusion (com.branchapp.memfusion.* labels + mem-fusion
# install dir only).
#
# Specifically:
#   1. Stops com.branchapp.memfusion.constellation, com.cowork.qdrant,
#      com.cowork.ollama
#   2. Moves the qdrant binary, qdrant-data tree, and .qdrant-initialized
#      marker from ~/.local/share/cowork-memory/ to ~/.local/share/mem-fusion/
#   3. Writes a fresh ~/.local/share/mem-fusion/qdrant-config.yaml using
#      absolute paths (no $HOME expansion required — avoids fix 0.5.0-001)
#   4. Writes new launchd plists for com.branchapp.memfusion.qdrant and
#      com.branchapp.memfusion.ollama
#   5. Removes the cowork plists; loads the new ones
#   6. Verifies qdrant + ollama come back up healthy
#   7. Restarts Constellation
#   8. Patches ~/.claude.json so any MCPs referencing the cowork-memory
#      venv now use mem-fusion's venv (literal sed — preserves formatting)
#   9. Archives the residual cowork-memory dir to cowork-memory.archived-<ts>
#
# Idempotent: if cowork-memory doesn't exist, exits 0.
# Conservative: refuses to migrate if target paths already have conflicting
# files (manual review required).
#
# Run via:
#   bash <path>/0.5.0-002-cowork-to-memfusion-rename.sh

set -euo pipefail

HOME_DIR="${HOME}"
COWORK="${HOME_DIR}/.local/share/cowork-memory"
MEMFUSION="${HOME_DIR}/.local/share/mem-fusion"
AGENTS="${HOME_DIR}/Library/LaunchAgents"
CLAUDE_JSON="${HOME_DIR}/.claude.json"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-002: Complete cowork → mem-fusion infrastructure rename"

# Idempotency
if [[ ! -d "$COWORK" ]]; then
  log "Already migrated: $COWORK does not exist."
  exit 0
fi

# Sanity checks
if [[ ! -f "$COWORK/bin/qdrant" ]] || [[ ! -d "$COWORK/qdrant-data" ]]; then
  log "ERROR: expected qdrant infrastructure missing in $COWORK. Aborting."
  exit 1
fi

for c in "$MEMFUSION/bin/qdrant" "$MEMFUSION/qdrant-data" "$MEMFUSION/qdrant-config.yaml"; do
  if [[ -e "$c" ]]; then
    log "ERROR: Target $c already exists. Manual review required."
    exit 1
  fi
done

# 1. Stop services
log "Stopping services (constellation + cowork qdrant/ollama)"
launchctl unload "$AGENTS/com.branchapp.memfusion.constellation.plist" 2>/dev/null || true
launchctl unload "$AGENTS/com.cowork.qdrant.plist" 2>/dev/null || true
launchctl unload "$AGENTS/com.cowork.ollama.plist" 2>/dev/null || true
sleep 2

# 2. Move qdrant infrastructure
log "Moving qdrant binary -> $MEMFUSION/bin/qdrant"
mkdir -p "$MEMFUSION/bin"
mv "$COWORK/bin/qdrant" "$MEMFUSION/bin/qdrant"

log "Moving qdrant-data tree -> $MEMFUSION/qdrant-data"
mv "$COWORK/qdrant-data" "$MEMFUSION/qdrant-data"

if [[ -f "$COWORK/.qdrant-initialized" ]]; then
  mv "$COWORK/.qdrant-initialized" "$MEMFUSION/.qdrant-initialized"
fi

# 3. Write new qdrant config with absolute paths (no $HOME / no env vars)
log "Writing $MEMFUSION/qdrant-config.yaml (absolute paths, no env expansion)"
cat > "$MEMFUSION/qdrant-config.yaml" <<EOF
storage:
  storage_path: ${MEMFUSION}/qdrant-data

snapshots_config:
  snapshots_path: ${MEMFUSION}/qdrant-data/snapshots

service:
  host: 127.0.0.1
  http_port: 6333
  grpc_port: 6334
  enable_cors: false

log_level: WARN
EOF

# 4. Write new launchd plists
log "Writing $AGENTS/com.branchapp.memfusion.qdrant.plist"
cat > "$AGENTS/com.branchapp.memfusion.qdrant.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.branchapp.memfusion.qdrant</string>
    <key>ProgramArguments</key>
    <array>
        <string>${MEMFUSION}/bin/qdrant</string>
        <string>--config-path</string>
        <string>${MEMFUSION}/qdrant-config.yaml</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>${MEMFUSION}/logs/qdrant.log</string>
    <key>StandardErrorPath</key>
    <string>${MEMFUSION}/logs/qdrant-error.log</string>
    <key>SoftResourceLimits</key>
    <dict><key>NumberOfFiles</key><integer>65536</integer></dict>
    <key>HardResourceLimits</key>
    <dict><key>NumberOfFiles</key><integer>65536</integer></dict>
    <key>WorkingDirectory</key>
    <string>${MEMFUSION}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>${HOME_DIR}</string>
        <key>MALLOC_CONF</key><string>background_thread:false</string>
    </dict>
</dict>
</plist>
EOF

log "Writing $AGENTS/com.branchapp.memfusion.ollama.plist"
cat > "$AGENTS/com.branchapp.memfusion.ollama.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.branchapp.memfusion.ollama</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/ollama</string>
        <string>serve</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>${MEMFUSION}/logs/ollama.log</string>
    <key>StandardErrorPath</key>
    <string>${MEMFUSION}/logs/ollama-error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>${HOME_DIR}</string>
        <key>OLLAMA_HOST</key><string>127.0.0.1:11434</string>
    </dict>
</dict>
</plist>
EOF

# 5. Remove cowork plists
log "Removing cowork plists"
rm -f "$AGENTS/com.cowork.qdrant.plist" "$AGENTS/com.cowork.ollama.plist"

# 6. Load new plists
log "Loading com.branchapp.memfusion.qdrant"
launchctl load "$AGENTS/com.branchapp.memfusion.qdrant.plist"
log "Loading com.branchapp.memfusion.ollama"
launchctl load "$AGENTS/com.branchapp.memfusion.ollama.plist"

# 7. Health checks
log "Waiting for Qdrant on http://127.0.0.1:6333/healthz"
if curl -s --retry 20 --retry-delay 1 --retry-connrefused http://127.0.0.1:6333/healthz >/dev/null; then
  log "  Qdrant healthy"
else
  log "  ERROR: Qdrant didn't start. Check ${MEMFUSION}/logs/qdrant-error.log"
  exit 1
fi

log "Waiting for Ollama on http://127.0.0.1:11434/api/tags"
if curl -s --retry 20 --retry-delay 1 --retry-connrefused http://127.0.0.1:11434/api/tags >/dev/null; then
  log "  Ollama healthy"
else
  log "  ERROR: Ollama didn't start. Check ${MEMFUSION}/logs/ollama-error.log"
  exit 1
fi

# 8. Restart Constellation
if [[ -f "$AGENTS/com.branchapp.memfusion.constellation.plist" ]]; then
  log "Reloading Constellation"
  launchctl load "$AGENTS/com.branchapp.memfusion.constellation.plist"
  if curl -s --retry 12 --retry-delay 1 --retry-connrefused http://127.0.0.1:7533/health >/dev/null; then
    log "  Constellation healthy"
  else
    log "  WARNING: Constellation did not come up cleanly"
  fi
fi

# 9. Patch ~/.claude.json to redirect cowork-memory venv references to mem-fusion
if [[ -f "$CLAUDE_JSON" ]] && grep -q "/cowork-memory/venv/" "$CLAUDE_JSON"; then
  log "Patching $CLAUDE_JSON: /cowork-memory/venv/ -> /mem-fusion/venv/"
  sed -i.bak-"${TIMESTAMP}" "s|/cowork-memory/venv/|/mem-fusion/venv/|g" "$CLAUDE_JSON"
  log "  Backup at ${CLAUDE_JSON}.bak-${TIMESTAMP}"
else
  log "No cowork-memory venv references in $CLAUDE_JSON (already clean)"
fi

# 10. Archive residual cowork-memory directory
ARCHIVED="${COWORK}.archived-${TIMESTAMP}"
log "Archiving residual cowork-memory: $COWORK -> $ARCHIVED"
mv "$COWORK" "$ARCHIVED"

log ""
log "Migration complete."
log "  Archived: $ARCHIVED"
log "  All services running under com.branchapp.memfusion.* labels"
log "  Verify with: launchctl list | grep memfusion"
log "  Once verified, remove archive: rm -rf $ARCHIVED"

exit 0
