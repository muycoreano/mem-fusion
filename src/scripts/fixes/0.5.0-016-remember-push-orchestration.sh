#!/usr/bin/env bash
# Fix 0.5.0-016 — /remember push <connector-id> orchestration primitives
#
# Implements engineer-lane work item 0.5.0-016 per architect-acked queue
# (docs/v0.5_ARCHITECT_ACK_2026-05-17.md "Suggested next ships"). Bundles:
#
#   - G3 (load_connectors_config schema validation)
#   - G5 (suggest-pull-on-declare prose in SKILL.md)
#   - G11 partial (is_smoke_test envelope marker docs in SKILL.md)
#
# DEPLOYS:
#
#   A. Patched core.py:
#      - load_connectors_config(path?) — validates connector.json shape
#        (required id/type, unique ids, known types per registry, type-
#        specific required fields). Returns {connectors, errors, warnings,
#        config_path}. Never raises.
#      - load_connectors_config_tool(args) — async MCP wrapper.
#      - build_connector_envelope(args) — composes load_config + export_
#        record + Connector.build_envelope_from_record + format_envelope.
#        Returns {body, channel, connector_id, type, submitted_at,
#        envelope, body_size} or classified error. Enforces the
#        substrate body cap (38_000 chars for Slack).
#      - get_connector_cursor_tool(args) / set_connector_cursor_tool(args)
#        — async MCP wrappers around the existing 0.5.0-014 sync helpers.
#      - CONNECTORS_CONFIG_PATH constant + MEMFUSION_CONNECTORS_CONFIG
#        env override (for test isolation).
#
#   B. Patched mem_fusion.py:
#      - Registers 4 new MCP tools: load_connectors_config,
#        build_connector_envelope, get_connector_cursor,
#        set_connector_cursor.
#      - Dispatch table entries added in lock-step.
#
#   C. SKILL.md additions (mirrored to local ~/.claude install):
#      - "Connector push orchestration (/remember push <connector-id>)"
#        — 5-step procedure that composes load_connectors_config →
#        search_memory(connector_id=...) → build_connector_envelope →
#        slack_search_channels (if needed) → slack_send_message →
#        set_connector_cursor.
#      - "Suggest-pull-on-declare" prose (G5).
#      - "Smoke-test envelopes (G11 marker)" prose.
#
# NOT IN SCOPE FOR THIS FIX:
#   - The actual Slack-side send. That's Claude orchestrating
#     slack_send_message via the Slack MCP layer; mem-fusion's process
#     never touches Slack auth or HTTP.
#   - /remember pull orchestration. Ships as 0.5.0-017 (separate commit)
#     after these primitives land. Pull-side receive flow already in
#     place via core.store_memory_from_envelope (Stage 1.1+1.2 + 1.3).
#   - The receive-side is_smoke_test filter. Lands with 0.5.0-017 pull
#     orchestration; sender-side marker is documented in this commit.
#
# Idempotent. Verified by tests/mem_fusion/test-connector-push-orchestration.py
# (25 checks: G3 validation cases, build_connector_envelope composition
# chain, cursor MCP round-trip, error classifications).
#
# Run via: bash 0.5.0-016-remember-push-orchestration.sh

set -euo pipefail

MEMFUSION="${HOME}/.local/share/mem-fusion"
CORE_PY="${MEMFUSION}/core.py"
MEM_FUSION_PY="${MEMFUSION}/mem_fusion.py"
VENV_PY="${MEMFUSION}/venv/bin/python"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SRC_CORE="${SRC_ROOT}/core.py"
SRC_MEM_FUSION="${SRC_ROOT}/mem_fusion.py"
SRC_SKILL="${SRC_ROOT}/skills/remember/SKILL.md"
LOCAL_SKILL="${HOME}/.claude/skills/remember/SKILL.md"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-016: /remember push <connector-id> orchestration primitives"

# Pre-flight
[[ -f "$CORE_PY" ]]        || { log "ERROR: $CORE_PY not found"; exit 1; }
[[ -f "$MEM_FUSION_PY" ]]  || { log "ERROR: $MEM_FUSION_PY not found"; exit 1; }
[[ -x "$VENV_PY" ]]        || { log "ERROR: $VENV_PY not found"; exit 1; }
[[ -f "$SRC_CORE" ]]       || { log "ERROR: $SRC_CORE not found — run from a mem-fusion repo checkout"; exit 1; }
[[ -f "$SRC_MEM_FUSION" ]] || { log "ERROR: $SRC_MEM_FUSION not found"; exit 1; }
[[ -f "$SRC_SKILL" ]]      || { log "ERROR: $SRC_SKILL not found"; exit 1; }

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix A — deploy patched core.py (load_connectors_config +
#             build_connector_envelope + cursor MCP wrappers)
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── A: deploy patched core.py (push-orchestration primitives) ──"
if grep -q '^async def build_connector_envelope' "$CORE_PY" \
   && grep -q '^def load_connectors_config' "$CORE_PY"; then
  log "  Already applied: build_connector_envelope + load_connectors_config present."
else
  BACKUP="${CORE_PY}.bak.0.5.0-016"
  log "  Backing up to $BACKUP"
  cp "$CORE_PY" "$BACKUP"
  log "  Deploying canonical $SRC_CORE → $CORE_PY"
  cp "$SRC_CORE" "$CORE_PY"
  "$VENV_PY" -c "import ast; ast.parse(open('$CORE_PY').read())" \
    && log "  syntax ok" \
    || { log "  ERROR: deployed core.py failed to parse — restoring backup"; cp "$BACKUP" "$CORE_PY"; exit 1; }
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix B — deploy patched mem_fusion.py (4 new MCP tools)
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── B: deploy patched mem_fusion.py (4 new MCP tools) ──"
if grep -q 'name="build_connector_envelope"' "$MEM_FUSION_PY"; then
  log "  Already applied: build_connector_envelope tool present in deployed mem_fusion.py."
else
  BACKUP="${MEM_FUSION_PY}.bak.0.5.0-016"
  log "  Backing up to $BACKUP"
  cp "$MEM_FUSION_PY" "$BACKUP"
  log "  Deploying canonical $SRC_MEM_FUSION → $MEM_FUSION_PY"
  cp "$SRC_MEM_FUSION" "$MEM_FUSION_PY"
  "$VENV_PY" -c "import ast; ast.parse(open('$MEM_FUSION_PY').read())" \
    && log "  syntax ok" \
    || { log "  ERROR: deployed mem_fusion.py failed to parse — restoring backup"; cp "$BACKUP" "$MEM_FUSION_PY"; exit 1; }
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix C — mirror SKILL.md to local ~/.claude install
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── C: mirror SKILL.md to local ~/.claude install ──"
if [[ -f "$LOCAL_SKILL" ]]; then
  if cmp -s "$SRC_SKILL" "$LOCAL_SKILL"; then
    log "  Already mirrored: local SKILL.md matches canonical."
  else
    BACKUP="${LOCAL_SKILL}.bak.0.5.0-016"
    log "  Backing up local to $BACKUP"
    cp "$LOCAL_SKILL" "$BACKUP"
    cp "$SRC_SKILL" "$LOCAL_SKILL"
    log "  Mirrored canonical SKILL.md → $LOCAL_SKILL"
  fi
else
  log "  WARNING: $LOCAL_SKILL not present — skipping mirror. Install ~/.claude/skills/remember/ first."
fi

# ──────────────────────────────────────────────────────────────────────────
# Verify
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── verify: new tools resolve via deployed core.py ──"
"$VENV_PY" <<'PY'
import os, sys
sys.path.insert(0, os.path.expanduser("~/.local/share/mem-fusion"))
import core
import inspect

required = [
    ("load_connectors_config",        False),  # sync
    ("load_connectors_config_tool",   True),
    ("build_connector_envelope",      True),
    ("get_connector_cursor_tool",     True),
    ("set_connector_cursor_tool",     True),
]
ok = True
for name, is_async in required:
    fn = getattr(core, name, None)
    if fn is None:
        print(f"  ✗ missing: core.{name}")
        ok = False
        continue
    if is_async and not inspect.iscoroutinefunction(fn):
        print(f"  ✗ not async: core.{name}")
        ok = False
        continue
    print(f"  ✓ {name}")

sys.exit(0 if ok else 1)
PY

log ""
log "==> 0.5.0-016 complete. Push-orchestration primitives deployed."
log "    New MCP tools (active on next Claude session): load_connectors_config,"
log "    build_connector_envelope, get_connector_cursor, set_connector_cursor."
log "    SKILL.md procedural section guides /remember push <connector-id>."
