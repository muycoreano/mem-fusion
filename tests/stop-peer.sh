#!/usr/bin/env bash
# Stop a dev peer's Qdrant.
# SIGTERM first, escalate to SIGKILL after 3s if still running.
#
# Usage: ./stop-peer.sh <peer-name>
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

PEER_NAME="${1:-}"
require_peer_name "$PEER_NAME"
PID_FILE="$(peer_pid_file "$PEER_NAME")"

if [[ ! -f "$PID_FILE" ]]; then
  echo "$PEER_NAME not running."
  exit 0
fi

PID="$(cat "$PID_FILE")"
kill "$PID" 2>/dev/null || true

# Wait up to 3 seconds for clean exit
for i in 1 2 3; do
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done

# Force kill if still alive
if kill -0 "$PID" 2>/dev/null; then
  kill -9 "$PID" 2>/dev/null || true
  echo "  (SIGKILL — process didn't exit cleanly)"
fi

rm -f "$PID_FILE"
echo "✓ Stopped $PEER_NAME"
