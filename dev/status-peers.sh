#!/usr/bin/env bash
# Show status of all known dev peers.
# Usage: ./status-peers.sh
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

printf "%-22s %-14s %-6s %-7s %s\n" "PEER" "STATUS" "PORT" "PID" "INSTALL"
printf "%-22s %-14s %-6s %-7s %s\n" "----" "------" "----" "---" "-------"

for peer in $KNOWN_PEERS; do
  port="$(peer_port "$peer")"
  install_dir="$(peer_dir "$peer")"
  status="—"
  pid="—"

  if [[ -d "$install_dir" ]]; then
    if peer_is_running "$peer"; then
      status="RUNNING"
      pid="$(cat "$(peer_pid_file "$peer")")"
    else
      status="stopped"
    fi
  else
    status="not installed"
  fi

  printf "%-22s %-14s %-6s %-7s %s\n" "$peer" "$status" "$port" "$pid" "$install_dir"
done
