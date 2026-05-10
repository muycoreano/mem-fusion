#!/usr/bin/env bash
# Fully remove a dev peer: stop it, delete install dir, unregister MCP.
# Destructive — wipes the peer's Qdrant data. Use confidently in dev.
#
# Usage: ./teardown-peer.sh <peer-name>
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

PEER_NAME="${1:-}"
require_peer_name "$PEER_NAME"

if peer_is_running "$PEER_NAME"; then
  echo "→ Stopping..."
  "$(dirname "$0")/stop-peer.sh" "$PEER_NAME"
fi

echo "→ Unregistering MCP..."
claude mcp remove "$PEER_NAME" 2>/dev/null || true

echo "→ Removing $(peer_dir "$PEER_NAME")..."
rm -rf "$(peer_dir "$PEER_NAME")"

echo "✓ Teardown complete: $PEER_NAME"
