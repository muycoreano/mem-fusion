#!/usr/bin/env bash
# Start a dev peer's Qdrant in the background.
# Writes the PID to <install-dir>/peer.pid; verifies healthcheck before reporting success.
#
# Usage: ./start-peer.sh <peer-name>
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

PEER_NAME="${1:-}"
require_peer_name "$PEER_NAME"
INSTALL_DIR="$(peer_dir "$PEER_NAME")"
PORT="$(peer_port "$PEER_NAME")"

[[ -d "$INSTALL_DIR" ]] || { echo "✗ $PEER_NAME not installed. Run setup-peer.sh $PEER_NAME first."; exit 1; }

if peer_is_running "$PEER_NAME"; then
  echo "Already running: $PEER_NAME (pid $(cat "$(peer_pid_file "$PEER_NAME")"))"
  exit 0
fi

# Background Qdrant; capture stdout/stderr to separate logs
nohup "$INSTALL_DIR/bin/qdrant" --config-path "$INSTALL_DIR/qdrant-config.yaml" \
  > "$INSTALL_DIR/logs/qdrant.log" 2> "$INSTALL_DIR/logs/qdrant-error.log" &
echo $! > "$(peer_pid_file "$PEER_NAME")"

# Wait for healthcheck (up to 6 seconds)
for i in 1 2 3 4 5 6; do
  curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done

if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null; then
  echo "✓ Started $PEER_NAME (pid $(cat "$(peer_pid_file "$PEER_NAME")"), qdrant on :$PORT)"
else
  echo "✗ Failed healthcheck. See $INSTALL_DIR/logs/qdrant-error.log"
  rm -f "$(peer_pid_file "$PEER_NAME")"
  exit 1
fi
