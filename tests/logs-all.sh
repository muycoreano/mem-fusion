#!/usr/bin/env bash
# Tail logs across every RUNNING dev peer.
# Useful when testing peer-to-peer behavior across multiple local peers.
#
# Usage: ./logs-all.sh
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

declare -a TAILS=()
for peer in $KNOWN_PEERS; do
  if peer_is_running "$peer"; then
    install_dir="$(peer_dir "$peer")"
    for log in qdrant.log qdrant-error.log mcp.log; do
      f="$install_dir/logs/$log"
      [[ -f "$f" ]] && TAILS+=("$f")
    done
  fi
done

if [[ ${#TAILS[@]} -eq 0 ]]; then
  echo "No peers running. Use ./status-peers.sh to see what's installed."
  exit 0
fi

echo "Tailing ${#TAILS[@]} log files across all running peers..."
echo "(tail -F prints the source filename when output switches between files)"
echo ""
exec tail -F "${TAILS[@]}"
