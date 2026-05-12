#!/usr/bin/env bash
# Shared library — sourced by every dev tooling script.
# Defines the peer name → port mapping, paths, and helpers.
#
# bash 3.2 compatible (the macOS system default). No associative arrays.

# ── Peer registry ─────────────────────────────────────────────────────────────
# Add a new peer by appending to KNOWN_PEERS AND adding a case below.
KNOWN_PEERS="mem-fusion-dev mem-fusion-peer-b mem-fusion-peer-c"

peer_port() {
  case "$1" in
    mem-fusion-dev)    echo 6433 ;;
    mem-fusion-peer-b) echo 6533 ;;
    mem-fusion-peer-c) echo 6633 ;;
    *)                 echo "" ;;
  esac
}

# Auto-detect prod install — supports both the post-rename canonical path
# and the legacy cowork-memory path that predates the v0.1.0 GitHub rename.
if [[ -d "$HOME/.local/share/mem-fusion" ]]; then
  PROD_DIR="$HOME/.local/share/mem-fusion"
elif [[ -d "$HOME/.local/share/cowork-memory" ]]; then
  PROD_DIR="$HOME/.local/share/cowork-memory"
else
  PROD_DIR=""   # caller will fail validation
fi

# ── Helpers ───────────────────────────────────────────────────────────────────
peer_dir()       { echo "$HOME/.local/share/$1"; }
peer_grpc_port() { echo $(( $(peer_port "$1") + 1 )); }
peer_collection(){ echo "cowork_memories"; }
peer_pid_file()  { echo "$(peer_dir "$1")/peer.pid"; }

peer_is_running() {
  local pid_file
  pid_file="$(peer_pid_file "$1")"
  [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null
}

require_peer_name() {
  if [[ -z "${1:-}" ]]; then
    echo "Usage: $(basename "$0") <peer-name>"
    echo "Known peers: $KNOWN_PEERS"
    exit 1
  fi
  if [[ -z "$(peer_port "$1")" ]]; then
    echo "Unknown peer: $1"
    echo "Known peers: $KNOWN_PEERS"
    exit 1
  fi
}
