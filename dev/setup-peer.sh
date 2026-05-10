#!/usr/bin/env bash
# One-time install of a dev peer.
# Creates ~/.local/share/<peer-name>/ with its own Qdrant config, MCP server,
# and registered MCP endpoint with Claude. NO launchd, NO hooks.
#
# Usage: ./setup-peer.sh <peer-name>
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

PEER_NAME="${1:-}"
require_peer_name "$PEER_NAME"

INSTALL_DIR="$(peer_dir "$PEER_NAME")"
PORT="$(peer_port "$PEER_NAME")"
GRPC_PORT="$(peer_grpc_port "$PEER_NAME")"
COLLECTION="$(peer_collection "$PEER_NAME")"

# ── Pre-flight ────────────────────────────────────────────────────────────────
[[ -d "$INSTALL_DIR" ]] && { echo "✗ $INSTALL_DIR already exists. Run teardown-peer.sh $PEER_NAME first."; exit 1; }
lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1 && { echo "✗ Port $PORT in use."; exit 1; }
[[ -d "$PROD_DIR" ]] || { echo "✗ Production mem-fusion not found at $PROD_DIR."; exit 1; }
curl -sf http://127.0.0.1:11434/api/tags | grep -q nomic-embed-text \
  || { echo "✗ Ollama not running or nomic-embed-text not loaded."; exit 1; }

echo "→ Installing $PEER_NAME"
echo "  install_dir: $INSTALL_DIR"
echo "  qdrant_port: $PORT (grpc: $GRPC_PORT)"
echo "  collection:  $COLLECTION"

# ── Layout ────────────────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR"/{bin,scripts,logs,queue,qdrant-data,snapshots,extensions}
cp "$PROD_DIR/bin/qdrant" "$INSTALL_DIR/bin/qdrant"
ln -s "$PROD_DIR/venv" "$INSTALL_DIR/venv"

# ── Qdrant config ─────────────────────────────────────────────────────────────
cat > "$INSTALL_DIR/qdrant-config.yaml" <<EOF
storage:
  storage_path: $INSTALL_DIR/qdrant-data
snapshots_config:
  snapshots_path: $INSTALL_DIR/qdrant-data/snapshots
service:
  host: 127.0.0.1
  http_port: $PORT
  grpc_port: $GRPC_PORT
  enable_cors: false
log_level: WARN
EOF

# ── Patched mcp_server.py ─────────────────────────────────────────────────────
sed -e "s|cowork_memories|$COLLECTION|g" \
    -e "s|mem_fusion_memories|$COLLECTION|g" \
    -e "s|http://127.0.0.1:6333|http://127.0.0.1:$PORT|g" \
    -e "s|\.local/share/mem-fusion|\.local/share/$PEER_NAME|g" \
    -e "s|\.local/share/cowork-memory|\.local/share/$PEER_NAME|g" \
    -e "s|Server(\"mem-fusion\")|Server(\"$PEER_NAME\")|g" \
    -e "s|Server(\"cowork-memory\")|Server(\"$PEER_NAME\")|g" \
    "$PROD_DIR/mcp_server.py" > "$INSTALL_DIR/mcp_server.py"

# ── Bootstrap Qdrant briefly to init the collection ──────────────────────────
echo "→ Initializing collection..."
"$INSTALL_DIR/bin/qdrant" --config-path "$INSTALL_DIR/qdrant-config.yaml" \
  > /dev/null 2> "$INSTALL_DIR/logs/qdrant-init.log" &
INIT_PID=$!
trap 'kill $INIT_PID 2>/dev/null || true' EXIT

# Wait for healthz
for i in 1 2 3 4 5; do
  curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null \
  || { echo "✗ Qdrant failed to start. See $INSTALL_DIR/logs/qdrant-init.log"; exit 1; }

MEMFUSION_COLLECTION="$COLLECTION" QDRANT_URL="http://127.0.0.1:$PORT" \
  "$PROD_DIR/venv/bin/python" "$(dirname "$0")/lib/init_collection.py"

kill $INIT_PID 2>/dev/null || true
wait $INIT_PID 2>/dev/null || true
trap - EXIT

# ── Register MCP with Claude ──────────────────────────────────────────────────
claude mcp add "$PEER_NAME" \
  "$PROD_DIR/venv/bin/python" "$INSTALL_DIR/mcp_server.py" \
  --env "MEMFUSION_COLLECTION=$COLLECTION" \
  --env "QDRANT_URL=http://127.0.0.1:$PORT" 2>&1 | tail -3

echo ""
echo "✓ Peer '$PEER_NAME' installed."
echo "  Start:  ./start-peer.sh $PEER_NAME"
echo "  Logs:   ./logs-peer.sh $PEER_NAME"
echo "  Status: ./status-peers.sh"
