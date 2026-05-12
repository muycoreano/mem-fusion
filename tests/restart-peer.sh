#!/usr/bin/env bash
# Stop + start a peer. Use after editing the peer's mcp_server.py or extensions.
# Usage: ./restart-peer.sh <peer-name>
set -euo pipefail
DIR="$(dirname "$0")"
"$DIR/stop-peer.sh" "$1"
"$DIR/start-peer.sh" "$1"
