#!/usr/bin/env bash
# Fix 0.5.0-008 — storage performance tuning bundle
#
# From local audit (mem-fusion memory 8aa91875-bc98-4c42-9e85-94de562cf75e):
# Authorized per direction-setting memory 0b26a3dc-f250-4f72-b1c3-a250e334e8d5.
#
# Bundles four independent fixes that share a "land them all at once because
# they're individually low-risk" profile. Each sub-step is idempotent on its
# own; running this script after any subset has been applied detects that
# state and skips. Total runtime: <5s on a healthy peer.
#
# SUB-FIXES:
#
# P3 — Qdrant hnsw_config.on_disk → true
#      Today the HNSW graph lives in RAM (`on_disk: false`). At single-user
#      scale this is invisible (~1 MB). At team scale (200/day shared), HNSW
#      RAM crosses 1 GB at year 1 and 5 GB at year 5. Flipping on_disk: true
#      mmaps the graph from disk; search latency goes from ~2 ms to ~5-10 ms
#      (still milliseconds, not seconds), RAM cost becomes near-zero.
#
# P4 — Qdrant scalar int8 quantization
#      Each vector currently consumes 768 × 4 = 3072 bytes. Scalar int8
#      quantization compresses to 768 bytes (4× reduction) with ~1% recall
#      loss. always_ram: true keeps the QUANTIZED vectors in RAM (cheap)
#      while originals live on disk. Compounds with P3 for combined ~10×
#      memory reduction at team scale.
#
# P5 — OLLAMA_KEEP_ALIVE=24h on the Ollama launchd plist
#      Ollama unloads idle models after 5 min default. Next embed call
#      pays ~3 s cold-load cost on the model re-warm. Setting KEEP_ALIVE=24h
#      pins the nomic-embed-text model in memory, eliminating cold-loads
#      under any normal-usage cadence.
#
# P7 — In-process LRU cache for embeddings in core.py
#      Identical or near-identical inputs (session-start primers, repeated
#      search_memory queries) currently re-embed each time at ~25 ms a pop.
#      Adds an OrderedDict-backed LRU cache (capacity 1000, ~3 MB RAM)
#      keyed by SHA-256 of the truncated text Ollama actually sees. Cache
#      hits return in ~30 µs (~800× speedup). Errors are never cached.
#
# WHAT'S DEFERRED (not in this bundle):
#   - P1 (indexing_threshold 20000→1000) and P2 (vacuum_min_vector_number
#     1000→50) — minimal effect at current 121-point scale; revisit when
#     the collection grows to 1K+ points.
#   - P6 — monitor-only.
#
# REVERSIBILITY:
#   - P3/P4: PATCH the collection back to the original values.
#   - P5: PlistBuddy Delete the OLLAMA_KEEP_ALIVE key + reload Ollama.
#   - P7: restore core.py from .bak-0.5.0-008 backup.
#
# Idempotent: re-running detects each sub-fix's state independently and
# skips already-applied work.
#
# Run via: bash 0.5.0-008-storage-perf-tuning.sh

set -euo pipefail

MEMFUSION="${HOME}/.local/share/mem-fusion"
CORE_PY="${MEMFUSION}/core.py"
VENV_PY="${MEMFUSION}/venv/bin/python"
OLLAMA_PLIST="${HOME}/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist"
QDRANT_BASE="http://127.0.0.1:6333"
COLLECTION="cowork_memories"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_CORE="$(cd "${SCRIPT_DIR}/../.." && pwd)/core.py"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-008: storage performance tuning bundle (P3 + P4 + P5 + P7)"

# ──────────────────────────────────────────────────────────────────────────
# Pre-flight
# ──────────────────────────────────────────────────────────────────────────
[[ -f "$CORE_PY" ]]     || { log "ERROR: $CORE_PY not found"; exit 1; }
[[ -x "$VENV_PY" ]]     || { log "ERROR: $VENV_PY not found"; exit 1; }
[[ -f "$OLLAMA_PLIST" ]] || { log "ERROR: $OLLAMA_PLIST not found"; exit 1; }
[[ -f "$SRC_CORE" ]]    || { log "ERROR: $SRC_CORE not found — run from a mem-fusion repo checkout"; exit 1; }
curl -s "${QDRANT_BASE}/healthz" >/dev/null || { log "ERROR: Qdrant unreachable at ${QDRANT_BASE}"; exit 1; }

# ──────────────────────────────────────────────────────────────────────────
# P3 + P4 — Qdrant collection config
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── P3 + P4: Qdrant HNSW on_disk + scalar int8 quantization ──"
STATE=$(curl -s "${QDRANT_BASE}/collections/${COLLECTION}")
ON_DISK=$(printf '%s' "$STATE" | "$VENV_PY" -c "import json,sys; print(json.load(sys.stdin)['result']['config']['hnsw_config'].get('on_disk', False))")
QUANT=$(printf '%s' "$STATE" | "$VENV_PY" -c "import json,sys; print('yes' if json.load(sys.stdin)['result']['config'].get('quantization_config') else 'no')")

if [[ "$ON_DISK" == "True" && "$QUANT" == "yes" ]]; then
  log "  Already applied: hnsw.on_disk=true and quantization_config set."
else
  log "  Current: hnsw.on_disk=${ON_DISK}  quantization_config=${QUANT}"
  log "  Applying PATCH..."
  RESP=$(curl -s -X PATCH "${QDRANT_BASE}/collections/${COLLECTION}" \
    -H 'Content-Type: application/json' \
    -d '{
      "hnsw_config": {"on_disk": true},
      "quantization_config": {
        "scalar": {"type": "int8", "quantile": 0.99, "always_ram": true}
      }
    }')
  OK=$(printf '%s' "$RESP" | "$VENV_PY" -c "import json,sys; print(json.load(sys.stdin).get('status'))")
  [[ "$OK" == "ok" ]] || { log "  ERROR: PATCH failed: $RESP"; exit 1; }
  log "  Applied. hnsw.on_disk=true, scalar-int8 quantization enabled."
fi

# ──────────────────────────────────────────────────────────────────────────
# P5 — Ollama OLLAMA_KEEP_ALIVE=24h
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── P5: Ollama OLLAMA_KEEP_ALIVE=24h ──"
if /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:OLLAMA_KEEP_ALIVE" "$OLLAMA_PLIST" >/dev/null 2>&1; then
  CURRENT=$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:OLLAMA_KEEP_ALIVE" "$OLLAMA_PLIST")
  if [[ "$CURRENT" == "24h" ]]; then
    log "  Already applied: OLLAMA_KEEP_ALIVE=24h already present in plist."
  else
    log "  Updating OLLAMA_KEEP_ALIVE: $CURRENT → 24h"
    /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OLLAMA_KEEP_ALIVE 24h" "$OLLAMA_PLIST"
    log "  Reloading Ollama..."
    launchctl unload "$OLLAMA_PLIST" 2>/dev/null || true
    sleep 1
    launchctl load "$OLLAMA_PLIST"
    launchctl kickstart -p "gui/$(id -u)/com.branchapp.memfusion.ollama" >/dev/null 2>&1 || true
    curl -s --retry 12 --retry-delay 1 --retry-connrefused "http://127.0.0.1:11434/api/tags" >/dev/null \
      && log "  Ollama healthy after reload." \
      || { log "  ERROR: Ollama failed to come back up"; exit 1; }
  fi
else
  log "  Adding OLLAMA_KEEP_ALIVE=24h to plist..."
  /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OLLAMA_KEEP_ALIVE string 24h" "$OLLAMA_PLIST"
  log "  Reloading Ollama..."
  launchctl unload "$OLLAMA_PLIST" 2>/dev/null || true
  sleep 1
  launchctl load "$OLLAMA_PLIST"
  launchctl kickstart -p "gui/$(id -u)/com.branchapp.memfusion.ollama" >/dev/null 2>&1 || true
  curl -s --retry 12 --retry-delay 1 --retry-connrefused "http://127.0.0.1:11434/api/tags" >/dev/null \
    && log "  Ollama healthy after reload." \
    || { log "  ERROR: Ollama failed to come back up"; exit 1; }
fi

# ──────────────────────────────────────────────────────────────────────────
# P7 — LRU embed cache in core.py (deploy canonical from repo src)
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── P7: LRU embed cache (deploy canonical core.py) ──"
if grep -q "^EMBED_CACHE_CAPACITY" "$CORE_PY"; then
  log "  Already applied: EMBED_CACHE_CAPACITY present in deployed core.py."
  # Sanity: if deployed differs from canonical (e.g., subsequent unrelated changes),
  # warn but don't override.
  if ! cmp -s "$SRC_CORE" "$CORE_PY"; then
    log "  WARNING: deployed core.py differs from canonical src/core.py — leaving deployed in place."
  fi
else
  BACKUP="${CORE_PY}.bak.0.5.0-008"
  log "  Backing up to $BACKUP"
  cp "$CORE_PY" "$BACKUP"
  log "  Deploying canonical $SRC_CORE → $CORE_PY"
  cp "$SRC_CORE" "$CORE_PY"
  # Verify the new file parses
  "$VENV_PY" -c "import ast; ast.parse(open('$CORE_PY').read())" \
    && log "  syntax ok" \
    || { log "  ERROR: deployed core.py failed to parse — restoring backup"; cp "$BACKUP" "$CORE_PY"; exit 1; }
  # Verify the cache symbol exists
  "$VENV_PY" - "$MEMFUSION" <<'PYVERIFY'
import sys, importlib.util
mf_dir = sys.argv[1]
spec = importlib.util.spec_from_file_location("core", f"{mf_dir}/core.py")
mod  = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert hasattr(mod, "EMBED_CACHE_CAPACITY"), "EMBED_CACHE_CAPACITY missing"
assert hasattr(mod, "_embed_cache"),         "_embed_cache missing"
print("  EMBED_CACHE_CAPACITY = %d ✓" % mod.EMBED_CACHE_CAPACITY)
print("  _embed_cache initialized ✓")
PYVERIFY
fi

# ──────────────────────────────────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────────────────────────────────
log ""
log "Fix 0.5.0-008 complete."
log "  Effect:"
log "    - Qdrant: HNSW graph mmaps from disk; scalar int8 quantization active."
log "    - Ollama: nomic-embed-text pinned via OLLAMA_KEEP_ALIVE=24h."
log "    - core.py: LRU embed cache (capacity 1000) active on next MCP connection."
log "  No further action required."
exit 0
