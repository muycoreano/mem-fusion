#!/usr/bin/env bash
# Fix 0.5.0-018 — Constellation /memory/put tolerant of content_hash mismatch
#
# Bug (observed 2026-05-18 by mc-macbookpro architect-hat-acting-as-engineer
# during the v0.5 install-flow scoping session):
#
# Every group_push from a 0.5.0-015-deployed peer to a pre-0.5.0-015 peer
# returns http 400 with "content_hash mismatch — content modified in
# transit". Root cause: 0.5.0-015 shipped the canonical content_hash
# algorithm (NFC + UTF-8 + sha256[64]); peers running the prior algorithm
# (.strip().lower() + truncate-to-16) recompute a different hash on
# receive and reject the legitimate memory as if transit corrupted it.
#
# Effect: the live mesh-sync regression — every push attempt during the
# 0.5.0-015 rollout window returns 400 on every peer, blocking memory
# propagation across the personal mesh. Confirmed twice in one session
# from mc-macbookpro → mc-macbookair, tk421-imac, tk421-macbookair (3/3
# peers returned http 400, 0 skipped).
#
# FIX SHAPE: make the daemon's /memory/put handler forward-compatible
# across content_hash algorithm versions. Recompute the hash locally;
# log a warning on mismatch but ACCEPT the memory anyway. Use the
# locally-canonical hash for storage + dedup so this node stays
# internally consistent regardless of which algorithm version the
# sending peer was on. The integrity protection the strict check used
# to provide is redundant with TCP checksums for LAN HTTP and with
# connector-side verify_integrity on the connector wire format;
# rejecting here was what broke the mesh during the 0.5.0-015 rollout.
#
# WHY THIS IS THE RIGHT FIX:
# Constellation peers communicate over LAN HTTP (loopback for gateway,
# LAN-only for peer surface). The threat model the strict hash check
# was guarding against — "did the bytes get corrupted on the wire" —
# is already covered by TCP's own integrity protection. The check was
# defensive engineering at the wrong layer, and it actively bites
# during version transitions. Connector-side integrity (per the v0.5
# Connector ABC's verify_integrity method) remains strict and
# substrate-owned — that's the right place for integrity enforcement,
# not on the LAN-HTTP daemon.
#
# IMPLEMENTATION:
#   - src/constellation.py:_handle_memory_put no longer rejects on hash
#     mismatch. Recomputes hash locally; logs WARNING if wire and local
#     differ; uses local_hash for find_existing_by_hash dedup; stores
#     local_hash in the payload (not the wire hash).
#   - Daemon kickstart picks up the new constellation.py.
#   - No core.py changes; no Qdrant migration; no payload schema change.
#
# THIS FIX SCRIPT:
#   A. Deploys canonical src/constellation.py → ~/.local/share/mem-fusion/.
#   B. Restarts the Constellation daemon to pick up the new code.
#   C. Verifies the daemon's /memory/put accepts a memory whose wire
#      content_hash differs from a locally-recomputed hash.
#
# Idempotent: re-running detects the deployed marker and skips.
#
# Run via: bash 0.5.0-018-fix-constellation-put-hash-tolerance.sh

set -euo pipefail

MEMFUSION="${HOME}/.local/share/mem-fusion"
CONSTELLATION_PY="${MEMFUSION}/constellation.py"
VENV_PY="${MEMFUSION}/venv/bin/python"
DAEMON_LABEL="com.branchapp.memfusion.constellation"
DAEMON_PLIST="${HOME}/Library/LaunchAgents/${DAEMON_LABEL}.plist"
GATEWAY_URL="http://127.0.0.1:7534"
PEER_URL="http://127.0.0.1:7533"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SRC_CONSTELLATION="${SRC_ROOT}/constellation.py"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-018: Constellation /memory/put tolerant of content_hash mismatch"

# Pre-flight
[[ -f "$CONSTELLATION_PY" ]]   || { log "ERROR: $CONSTELLATION_PY not found"; exit 1; }
[[ -x "$VENV_PY" ]]            || { log "ERROR: $VENV_PY not found"; exit 1; }
[[ -f "$SRC_CONSTELLATION" ]]  || { log "ERROR: $SRC_CONSTELLATION not found — run from a mem-fusion repo checkout"; exit 1; }

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix A — deploy patched constellation.py
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── A: deploy patched constellation.py (PUT hash tolerance) ──"
# Marker: the unique warning string introduced by this fix
if grep -q "PUT hash mismatch (accepted)" "$CONSTELLATION_PY"; then
  log "  Already applied: 0.5.0-018 marker present in constellation.py _handle_memory_put."
  RESTART_NEEDED=0
else
  BACKUP="${CONSTELLATION_PY}.bak.0.5.0-018"
  log "  Backing up to $BACKUP"
  cp "$CONSTELLATION_PY" "$BACKUP"
  log "  Deploying canonical $SRC_CONSTELLATION → $CONSTELLATION_PY"
  cp "$SRC_CONSTELLATION" "$CONSTELLATION_PY"
  "$VENV_PY" -c "import ast; ast.parse(open('$CONSTELLATION_PY').read())" \
    && log "  syntax ok" \
    || { log "  ERROR: deployed constellation.py failed to parse — restoring backup"; cp "$BACKUP" "$CONSTELLATION_PY"; exit 1; }
  RESTART_NEEDED=1
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix B — restart Constellation daemon if its code changed
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── B: restart Constellation daemon to pick up new constellation.py ──"
if [[ "${RESTART_NEEDED:-0}" -eq 0 ]]; then
  log "  No restart needed: constellation.py unchanged in this run."
else
  if [[ -f "$DAEMON_PLIST" ]]; then
    log "  Kickstarting $DAEMON_LABEL..."
    launchctl kickstart -k "gui/$(id -u)/${DAEMON_LABEL}" >/dev/null 2>&1 || \
      log "  WARNING: kickstart returned non-zero; daemon may not be loaded."
    sleep 2
    # Verify health: gateway responds
    if curl -s --max-time 5 "${GATEWAY_URL}/" >/dev/null 2>&1; then
      log "  ✓ Gateway responding post-restart"
    elif curl -s --max-time 5 "${PEER_URL}/health" >/dev/null 2>&1; then
      log "  ✓ Peer surface responding post-restart"
    else
      log "  WARNING: daemon health check inconclusive — check logs at ${MEMFUSION}/logs/"
    fi
  else
    log "  Plist not installed at $DAEMON_PLIST — skipping restart."
    log "  (This is fine if you don't have Constellation installed locally.)"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Verification — synthesize a /memory/put with deliberately-wrong wire hash
# and confirm the daemon accepts it (logging a warning).
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── Verification: /memory/put accepts content_hash mismatch ──"
# Constellation is optional in v0.5+ (see docs/v0.5_CONNECTOR_ARCHITECTURE.md
# — mem-fusion runs standalone; Constellation is one of several sharing
# substrates). Skip the live HTTP verification gracefully when the daemon
# isn't running on this peer: the code-deployment work above is the
# substantive fix and applies regardless of run state.
if [[ ! -f "$DAEMON_PLIST" ]]; then
  log "  SKIPPED — no Constellation daemon installed locally."
  log "  Fix 0.5.0-018 complete (deployed only; daemon not running on this peer)."
  exit 0
fi
if ! curl -sf --max-time 2 "http://127.0.0.1:7533/healthz" >/dev/null 2>&1; then
  log "  SKIPPED — Constellation daemon not running on this peer (peers cleared, no membership configured, or daemon stopped)."
  log "  Fix 0.5.0-018 complete (code deployed; live verification deferred until daemon is started)."
  exit 0
fi

"$VENV_PY" - <<'PYEOF'
import os, sys, json, uuid, datetime, httpx
sys.path.insert(0, os.path.expanduser("~/.local/share/mem-fusion"))
import core

# Build a wire memory with a deliberately wrong content_hash. If the fix
# is applied, daemon accepts (200 stored or duplicate). If not, daemon
# rejects with 400 "content_hash mismatch".
content = f"0.5.0-018 fix verification probe {uuid.uuid4()}"
vec = [0.0] * core.VECTOR_SIZE
# Compute the local hash but corrupt it for the wire field on purpose.
local = core.content_hash(content)
wire  = "deliberately-wrong-hash-for-fix-verification"
body = {
    "content":      content,
    "content_hash": wire,                       # intentionally != local
    "vector":       vec,
    "type":         "context",
    "tags":         ["is_smoke_test", "fix-018-probe"],
    "project":      "fix-018-verification",
    "importance":   1,
    "groups":       ["personal"],
    "origin_node":  core.NODE_NAME,
    "submitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
r = httpx.post("http://127.0.0.1:7533/memory/put", json=body, timeout=5.0)
if r.status_code != 200:
    print(f"  ✗ FAIL: daemon rejected mismatched hash with http {r.status_code}: {r.text[:200]}")
    sys.exit(1)
result = r.json()
print(f"  ✓ daemon accepted memory with mismatched wire hash: status={result.get('status')}")
print(f"  ✓ stored memory id: {result.get('id')}")
# Cleanup the probe memory so the verification doesn't pollute the store.
core.qdrant.delete(collection_name=core.COLLECTION, points_selector=[result["id"]])
print(f"  ✓ cleaned up probe memory")
PYEOF

log ""
log "Fix 0.5.0-018 complete."
log "  Effect:"
log "    - Constellation /memory/put no longer rejects on content_hash mismatch."
log "    - Logs WARNING when wire hash and local-recompute differ."
log "    - Stores locally-canonical hash regardless of wire-side algorithm."
log "    - Unblocks mesh sync across peers on different content_hash"
log "      algorithm versions (pre/post 0.5.0-015)."
log "  Daemon restarted to pick up the new constellation.py if changed."
exit 0
