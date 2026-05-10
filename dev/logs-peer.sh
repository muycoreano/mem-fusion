#!/usr/bin/env bash
# Tail logs for one dev peer.
#
# Usage:
#   ./logs-peer.sh <peer-name>            # all logs combined
#   ./logs-peer.sh <peer-name> qdrant     # qdrant.log only
#   ./logs-peer.sh <peer-name> errors     # qdrant-error.log only
#   ./logs-peer.sh <peer-name> mcp        # mcp.log only (server's own python logging)
#   ./logs-peer.sh <peer-name> ingest     # ingest.log only (session ingestion hook)
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

PEER_NAME="${1:-}"
STREAM="${2:-all}"
require_peer_name "$PEER_NAME"
LOGS="$(peer_dir "$PEER_NAME")/logs"

[[ -d "$LOGS" ]] || { echo "✗ No logs dir for $PEER_NAME (peer not installed?)"; exit 1; }

case "$STREAM" in
  qdrant) exec tail -F "$LOGS/qdrant.log" ;;
  errors) exec tail -F "$LOGS/qdrant-error.log" ;;
  mcp)    exec tail -F "$LOGS/mcp.log" ;;
  ingest) exec tail -F "$LOGS/ingest.log" ;;
  all)
    # tail -F supports multiple files; it labels each block when output switches sources
    cd "$LOGS"
    files=()
    for f in qdrant.log qdrant-error.log mcp.log ingest.log; do
      [[ -f "$f" ]] && files+=("$f")
    done
    [[ ${#files[@]} -eq 0 ]] && { echo "No log files exist yet (peer hasn't been started?)"; exit 1; }
    exec tail -F "${files[@]}"
    ;;
  *)
    echo "Usage: $0 <peer-name> [all|qdrant|errors|mcp|ingest]"
    exit 1
    ;;
esac
