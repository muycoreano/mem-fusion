#!/usr/bin/env bash
# Fix 0.5.0-012 — node-name normalization
#
# Background (from audit + role-clarification memory 0b26a3dc, and verified
# live during 0.5.0-011 debugging on mc-macbookair):
#
# core.py used to compute `NODE_NAME = _resolve_node_name()` once at module
# load and cache that value for the lifetime of the MCP-server subprocess.
# If the Constellation config.json did not exist at the moment the
# subprocess started, the fallback resolved to socket.gethostname() and
# stayed cached. Subsequent stores tagged origin_node with the hostname
# (e.g., `brmaclap0480`) instead of the Constellation config's node_name
# (e.g., `mc-macbookair`). Even after the config was created, the running
# MCP server continued to use the stale cached value.
#
# Consequences observed live:
#   - Pass 2's self-filter compares against the freshly-resolved config
#     name; cached-hostname-tagged memories from this peer surface as
#     "received from peer" — user-visible noise on every prompt.
#   - Constellation loopback prevention partially breaks: pulled memories
#     tagged with our hostname do not match self-resolution; saved from
#     duplication only by content_hash dedup.
#   - Future SlackConnector loopback (per architecture doc §6.2) inherits
#     the same bug shape until normalized.
#
# THIS FIX (two-part):
#
#   A. Going-forward correctness: deploy a patched core.py that calls
#      _resolve_node_name() on EVERY store/find_or_create instead of caching
#      at module-load. Re-resolve cost is one stat() + optional json.load
#      per write — sub-millisecond, dwarfed by Qdrant upsert and embedding.
#      Marker: `"origin_node": _resolve_node_name()` appears in core.py.
#
#   B. One-shot migration: rewrite payload `origin_node` on existing
#      Qdrant points where origin_node equals THIS peer's hostname (and
#      only that exact hostname — other peers' hostname-tagged entries
#      are their responsibility to migrate on their own machines via this
#      same script). Target: this peer's Constellation config.node_name.
#      No-op if hostname == config.node_name. Idempotent.
#
# IMPORTANT — MCP server caching caveat:
# The currently-running mem-fusion MCP server subprocess has the old core.py
# cached in its Python module cache. The deploy in step A takes effect for
# any NEW MCP connection (next Claude session). Writes made during the
# current session by the still-running subprocess will continue to use the
# stale NODE_NAME constant until the subprocess restarts. The migration in
# step B can be safely re-run after a session restart to catch any
# stragglers; it's idempotent.
#
# Run via: bash 0.5.0-012-fix-node-name-normalization.sh

set -euo pipefail

MEMFUSION="${HOME}/.local/share/mem-fusion"
CORE_PY="${MEMFUSION}/core.py"
VENV_PY="${MEMFUSION}/venv/bin/python"
CONFIG_JSON="${MEMFUSION}/constellation/config.json"
QDRANT_BASE="http://127.0.0.1:6333"
COLLECTION="cowork_memories"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_CORE="$(cd "${SCRIPT_DIR}/../.." && pwd)/core.py"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-012: node-name normalization (core.py + payload migration)"

# Pre-flight
[[ -f "$CORE_PY" ]]      || { log "ERROR: $CORE_PY not found"; exit 1; }
[[ -x "$VENV_PY" ]]      || { log "ERROR: $VENV_PY not found"; exit 1; }
[[ -f "$CONFIG_JSON" ]]  || { log "ERROR: $CONFIG_JSON not found — Constellation config required"; exit 1; }
[[ -f "$SRC_CORE" ]]     || { log "ERROR: $SRC_CORE not found — run from a mem-fusion repo checkout"; exit 1; }
curl -s "${QDRANT_BASE}/healthz" >/dev/null || { log "ERROR: Qdrant unreachable"; exit 1; }

# Resolve names — must match the running peer's identity.
HOSTNAME_VAL=$(hostname)
CFG_NODE_NAME=$("$VENV_PY" -c "import json; print(json.load(open('$CONFIG_JSON'))['node_name'])")
log "  hostname:               $HOSTNAME_VAL"
log "  config node_name:       $CFG_NODE_NAME"

if [[ "$HOSTNAME_VAL" == "$CFG_NODE_NAME" ]]; then
  log "  Hostname matches config node_name — no normalization needed."
  log "  (Will still deploy patched core.py if not already current.)"
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix A — deploy patched core.py
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── A: deploy patched core.py with per-write _resolve_node_name() ──"
# Marker: the new pattern `_resolve_node_name()` in the store_memory payload
# (it didn't exist in pre-0.5.0-012 core.py; the constant NODE_NAME was used).
if grep -q '"origin_node": _resolve_node_name()' "$CORE_PY"; then
  log "  Already applied: _resolve_node_name() present in core.py write paths."
  if ! cmp -s "$SRC_CORE" "$CORE_PY"; then
    log "  Note: deployed core.py differs from canonical — keeping deployed (other unrelated edits)."
  fi
else
  BACKUP="${CORE_PY}.bak.0.5.0-012"
  log "  Backing up to $BACKUP"
  cp "$CORE_PY" "$BACKUP"
  log "  Deploying canonical $SRC_CORE → $CORE_PY"
  cp "$SRC_CORE" "$CORE_PY"
  "$VENV_PY" -c "import ast; ast.parse(open('$CORE_PY').read())" \
    && log "  syntax ok" \
    || { log "  ERROR: deployed core.py failed to parse — restoring backup"; cp "$BACKUP" "$CORE_PY"; exit 1; }
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix B — one-shot payload migration
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── B: migrate Qdrant payloads (origin_node $HOSTNAME_VAL → $CFG_NODE_NAME) ──"

"$VENV_PY" - "$HOSTNAME_VAL" "$CFG_NODE_NAME" "$QDRANT_BASE" "$COLLECTION" <<'PYEOF'
import sys, json
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

hostname, target, qdrant_url, collection = sys.argv[1:5]

if hostname == target:
    print(f"  No migration needed: hostname == config.node_name == {target!r}")
    sys.exit(0)

client = QdrantClient(url=qdrant_url, timeout=10)

# Count first so we can log progress meaningfully.
filter_ = Filter(must=[FieldCondition(key="origin_node", match=MatchValue(value=hostname))])
count_resp = client.count(collection_name=collection, count_filter=filter_, exact=True)
total = count_resp.count
print(f"  Found {total} memory(ies) tagged with origin_node={hostname!r}")

if total == 0:
    print(f"  Already migrated: nothing to update.")
    sys.exit(0)

# Scroll and rewrite. Pages are cheap; we keep page size modest to bound
# transient set_payload load.
PAGE = 128
migrated = 0
offset = None
while True:
    pts, offset = client.scroll(
        collection_name=collection,
        scroll_filter=filter_,
        limit=PAGE,
        with_payload=False,
        with_vectors=False,
        offset=offset,
    )
    if not pts:
        break
    ids = [p.id for p in pts]
    client.set_payload(
        collection_name=collection,
        payload={"origin_node": target},
        points=ids,
    )
    migrated += len(ids)
    print(f"  migrated {migrated}/{total}...")
    if offset is None:
        break

# Verify migration completeness
post = client.count(collection_name=collection, count_filter=filter_, exact=True).count
if post != 0:
    print(f"  WARNING: {post} memories still tagged with {hostname!r} after migration.")
    print(f"           (Possible if new writes happened mid-migration. Re-run the script.)")
    sys.exit(0)

# Verify target count grew (sanity)
target_filter = Filter(must=[FieldCondition(key="origin_node", match=MatchValue(value=target))])
target_count = client.count(collection_name=collection, count_filter=target_filter, exact=True).count
print(f"  ✓ Migration complete: 0 memories tagged {hostname!r}, {target_count} now tagged {target!r}")
PYEOF

log ""
log "Fix 0.5.0-012 complete."
log "  CAVEAT: the currently-running MCP server subprocess may still have"
log "          NODE_NAME cached. Writes from THIS session continue to use the"
log "          stale value until the next Claude session restarts the MCP server."
log "          Re-run this script after a session restart to migrate any stragglers."
log "          The migration is idempotent."
exit 0
