#!/usr/bin/env bash
# Fix 0.5.0-014 — v0.5 connector_ids field + cursor file scaffolding
#
# Implements engineer-lane work items E-A, E-B, E-C from the architect's
# action item (memory 0daf2f0c, 2026-05-17). Architecture contract:
# docs/v0.5_CONNECTOR_ARCHITECTURE.md §4 Memory Model.
#
# DEPLOYS:
#
#   A. Patched core.py (E-A):
#      - store_memory and find_or_create accept `connector_ids: list[str]`,
#        persist on payload, additive-union on dedup-merge.
#      - format_results (search_memory output) + search_recent output +
#        export_record all include `connector_ids` field. Default missing → [].
#      - build_filter accepts `connector_id` kwarg → search_memory filters
#        by Qdrant keyword-list match against `connector_ids`.
#      - entry_connector_ids() + normalize_connector_ids_arg() helpers
#        (parallels to v0.4 entry_groups / normalize_groups_arg).
#      - v0.4 `groups` field continues to deserialize without error and is
#        ignored at scope-determination per architecture doc §4 Migration.
#
#   B. Per-connector cursor persistence (E-B):
#      - core.py: get_connector_cursor(connector_id) / set_connector_cursor(
#        connector_id, iso_ts). File: ~/.local/share/mem-fusion/connector_cursors.json.
#        Atomic write via tempfile + os.replace. Graceful degradation on
#        missing file, missing key, corrupt JSON.
#      - Scaffold the empty cursor file at deploy time (idempotent).
#
#   C. add_connector_ids MCP tool (E-C):
#      - core.py: add_connector_ids(args) — additive union, same shape as
#        v0.4 add_groups. Returns {updated, no_op, errors}.
#      - mem_fusion.py: registers the tool with the MCP server's tool list
#        and dispatch table.
#
#   D. Patched src/skills/remember/SKILL.md (post-E-A clean-up):
#      - Command surface table updated to use `<name>` consistently
#        (resolves via connector → constellation-group dispatch).
#      - Bare `/remember push` / `/remember pull` documented as invalid
#        in v0.5 (privacy invariant: outbound propagation requires
#        explicit naming).
#      - Legacy v0.4 behaviors flagged in a transition section.
#
#   E. Qdrant `connector_ids` payload index:
#      - Creates a keyword payload index on the `connector_ids` field at
#        the deployed Qdrant for efficient search_memory(connector_id=…)
#        filtering. Idempotent (Qdrant errors on already-exists are
#        swallowed).
#
# NOT IN SCOPE FOR THIS FIX:
#   - The connector_push / connector_pull skill orchestration logic in
#     /remember (per §6 of architecture doc, that's E-D smoke-tested by
#     hand on this peer and lands in a follow-up commit when the user
#     authorizes connector.json declarations and per-connector flows.
#   - Migration of existing memories' payloads: v0.4 entries continue
#     to work with absent `connector_ids` → []. No re-write needed.
#
# Idempotent. Each sub-step has its own marker. Verified E-A/B/C tests
# pass against the live install (tests/mem_fusion/test-connector-ids.py).
#
# Run via: bash 0.5.0-014-connector-ids-field.sh

set -euo pipefail

MEMFUSION="${HOME}/.local/share/mem-fusion"
CORE_PY="${MEMFUSION}/core.py"
MEM_FUSION_PY="${MEMFUSION}/mem_fusion.py"
VENV_PY="${MEMFUSION}/venv/bin/python"
CURSOR_FILE="${MEMFUSION}/connector_cursors.json"
QDRANT_BASE="http://127.0.0.1:6333"
COLLECTION="cowork_memories"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SRC_CORE="${SRC_ROOT}/core.py"
SRC_MEM_FUSION="${SRC_ROOT}/mem_fusion.py"
SRC_SKILL="${SRC_ROOT}/skills/remember/SKILL.md"
LOCAL_SKILL="${HOME}/.claude/skills/remember/SKILL.md"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-014: v0.5 connector_ids field (E-A + E-B + E-C + SKILL.md + index)"

# Pre-flight
[[ -f "$CORE_PY" ]]       || { log "ERROR: $CORE_PY not found"; exit 1; }
[[ -f "$MEM_FUSION_PY" ]] || { log "ERROR: $MEM_FUSION_PY not found"; exit 1; }
[[ -x "$VENV_PY" ]]       || { log "ERROR: $VENV_PY not found"; exit 1; }
[[ -f "$SRC_CORE" ]]      || { log "ERROR: $SRC_CORE not found — run from a mem-fusion repo checkout"; exit 1; }
[[ -f "$SRC_MEM_FUSION" ]]|| { log "ERROR: $SRC_MEM_FUSION not found"; exit 1; }
[[ -f "$SRC_SKILL" ]]     || { log "ERROR: $SRC_SKILL not found"; exit 1; }
curl -s "${QDRANT_BASE}/healthz" >/dev/null || { log "ERROR: Qdrant unreachable at ${QDRANT_BASE}"; exit 1; }

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix A — deploy patched core.py (E-A + E-B + E-C functions all in core)
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── A: deploy patched core.py (E-A + E-B + E-C functions) ──"
# Marker: the new function add_connector_ids is uniquely identifiable
if grep -q "^async def add_connector_ids" "$CORE_PY"; then
  log "  Already applied: add_connector_ids present in deployed core.py."
else
  BACKUP="${CORE_PY}.bak.0.5.0-014"
  log "  Backing up to $BACKUP"
  cp "$CORE_PY" "$BACKUP"
  log "  Deploying canonical $SRC_CORE → $CORE_PY"
  cp "$SRC_CORE" "$CORE_PY"
  "$VENV_PY" -c "import ast; ast.parse(open('$CORE_PY').read())" \
    && log "  syntax ok" \
    || { log "  ERROR: deployed core.py failed to parse — restoring backup"; cp "$BACKUP" "$CORE_PY"; exit 1; }
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix B — deploy patched mem_fusion.py (registers add_connector_ids tool)
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── B: deploy patched mem_fusion.py (add_connector_ids MCP tool) ──"
if grep -q 'name == "add_connector_ids"' "$MEM_FUSION_PY"; then
  log "  Already applied: add_connector_ids dispatch present."
else
  BACKUP="${MEM_FUSION_PY}.bak.0.5.0-014"
  log "  Backing up to $BACKUP"
  cp "$MEM_FUSION_PY" "$BACKUP"
  log "  Deploying canonical $SRC_MEM_FUSION → $MEM_FUSION_PY"
  cp "$SRC_MEM_FUSION" "$MEM_FUSION_PY"
  "$VENV_PY" -c "import ast; ast.parse(open('$MEM_FUSION_PY').read())" \
    && log "  syntax ok" \
    || { log "  ERROR: deployed mem_fusion.py failed to parse — restoring backup"; cp "$BACKUP" "$MEM_FUSION_PY"; exit 1; }
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix C — Qdrant connector_ids payload index
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── C: ensure Qdrant connector_ids payload index ──"
INDEX_STATE=$(curl -s "${QDRANT_BASE}/collections/${COLLECTION}")
HAS_INDEX=$(printf '%s' "$INDEX_STATE" | "$VENV_PY" -c "
import json, sys
d = json.load(sys.stdin)['result']
schema = d.get('payload_schema', {})
print('yes' if 'connector_ids' in schema else 'no')
")
if [[ "$HAS_INDEX" == "yes" ]]; then
  log "  Already applied: connector_ids payload index present."
else
  log "  Creating connector_ids keyword payload index..."
  RESP=$(curl -s -X PUT "${QDRANT_BASE}/collections/${COLLECTION}/index?wait=true" \
    -H 'Content-Type: application/json' \
    -d '{"field_name": "connector_ids", "field_schema": "keyword"}')
  STATUS=$(printf '%s' "$RESP" | "$VENV_PY" -c "import json,sys; print(json.load(sys.stdin).get('status'))" 2>/dev/null || echo "?")
  if [[ "$STATUS" == "ok" ]]; then
    log "  ✓ index created"
  else
    log "  WARNING: index creation returned: $RESP"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix D — scaffold the cursor file (empty if absent)
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── D: scaffold connector_cursors.json (no overwrite if present) ──"
if [[ -f "$CURSOR_FILE" ]]; then
  log "  Already exists at $CURSOR_FILE — leaving in place."
else
  echo '{}' > "$CURSOR_FILE"
  log "  Created empty $CURSOR_FILE"
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix E — mirror updated SKILL.md to local ~/.claude install
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── E: mirror SKILL.md to local ~/.claude install ──"
if [[ -f "$LOCAL_SKILL" ]]; then
  if cmp -s "$SRC_SKILL" "$LOCAL_SKILL"; then
    log "  Already mirrored: local SKILL.md matches canonical."
  else
    BACKUP="${LOCAL_SKILL}.bak.0.5.0-014"
    log "  Backing up local to $BACKUP"
    cp "$LOCAL_SKILL" "$BACKUP"
    cp "$SRC_SKILL" "$LOCAL_SKILL"
    log "  Mirrored $SRC_SKILL → $LOCAL_SKILL"
  fi
else
  log "  No local SKILL.md found — skipping mirror (mem-fusion skill not installed here?)"
fi

# ──────────────────────────────────────────────────────────────────────────
# Verification
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── Verification ──"
"$VENV_PY" - "$MEMFUSION" "$CURSOR_FILE" <<'PYEOF'
import sys, importlib.util, os
mf_dir, cursor_file = sys.argv[1:3]
os.environ["MEMFUSION_CONNECTOR_CURSORS"] = cursor_file
spec = importlib.util.spec_from_file_location("core", f"{mf_dir}/core.py")
mod  = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert hasattr(mod, "add_connector_ids"),         "add_connector_ids missing"
assert hasattr(mod, "entry_connector_ids"),       "entry_connector_ids missing"
assert hasattr(mod, "normalize_connector_ids_arg"), "normalize_connector_ids_arg missing"
assert hasattr(mod, "get_connector_cursor"),      "get_connector_cursor missing"
assert hasattr(mod, "set_connector_cursor"),      "set_connector_cursor missing"
import inspect
sig = inspect.signature(mod.build_filter)
assert "connector_id" in sig.parameters, "build_filter missing connector_id"
print("  add_connector_ids exposed")
print("  entry_connector_ids / normalize_connector_ids_arg exposed")
print("  get/set_connector_cursor exposed")
print("  build_filter(connector_id=...) signature present")
PYEOF

log ""
log "Fix 0.5.0-014 complete."
log "  Effect:"
log "    - core.py: store_memory + find_or_create accept connector_ids; search_memory"
log "      filters by connector_id; search_recent + export_record expose the field."
log "    - mem_fusion.py: add_connector_ids MCP tool registered."
log "    - Qdrant: connector_ids payload index in place for efficient filtering."
log "    - connector_cursors.json scaffolded (empty if absent)."
log "    - SKILL.md mirrored to local ~/.claude install with v0.5 command surface."
log ""
log "  Next steps (E-D end-to-end smoke validated 2026-05-17; deferred to follow-up commit):"
log "    - /remember push <connector-id> and /remember pull <connector-id> skill orchestration"
log "      in src/skills/remember/SKILL.md per architecture doc §6."
log "    - Declare a real connector in ~/.local/share/mem-fusion/connector.json to enable."
log ""
log "  No MCP-server restart required: new tools are discoverable on the next"
log "  MCP connection (next Claude session). Already-active sessions continue with"
log "  the prior tool list cached until they end."
exit 0
