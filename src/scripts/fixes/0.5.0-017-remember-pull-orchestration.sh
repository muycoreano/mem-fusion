#!/usr/bin/env bash
# Fix 0.5.0-017 — /remember pull <connector-id> orchestration primitive
#
# Symmetric to 0.5.0-016 (push). Adds the receive-side composition
# primitive that powers /remember pull, plus the SKILL.md procedural
# section that guides Claude through the orchestration. Also lands
# G11 receive-side smoke-test filter — envelopes with is_smoke_test:
# true are skipped by default unless include_smoke_tests=true is
# explicitly passed.
#
# DEPLOYS:
#
#   A. Patched core.py:
#      - ingest_connector_message({body, connector_id,
#         include_smoke_tests?}) — composes Connector.parse_envelope +
#        G11 smoke-test filter + store_memory_from_envelope. Returns
#        outcomes: stored / merged / duplicate / loopback_skipped /
#        smoke_test_skipped / not_envelope / {error: ...}. Each outcome
#        (except errors and not_envelope) carries submitted_at so the
#        caller can advance the cursor at end-of-pull.
#
#   B. Patched mem_fusion.py:
#      - Registers ingest_connector_message MCP tool. Description
#        oriented to Claude's reasoning inside the skill (composes with
#        slack_read_channel, max submitted_at across stored outcomes for
#        cursor advancement, default G11 filter behavior).
#      - Dispatch table entry added.
#
#   C. SKILL.md additions (mirrored to local ~/.claude install):
#      - "Connector pull orchestration (/remember pull <connector-id>)"
#        — 9-step procedure: load+validate config → resolve connector →
#        resolve channel id → optional bot-invite preflight → get
#        cursor → compute Slack `oldest` (with first_pull_max_age_days
#        knob support) → paginate slack_read_channel → ingest_connector_
#        message per body → advance cursor on completion → render
#        per-outcome telemetry.
#      - "G11 smoke-test filter (receive-side)" prose.
#      - "First-pull semantics" prose with first_pull_max_age_days
#        knob documentation per architect ack §B.1.
#      - "What the orchestration does NOT do" — no parallel pulls, no
#        Slack-posted progress, no retry loop.
#
# NOT IN SCOPE FOR THIS FIX:
#   - The actual slack_read_channel calls and pagination — Claude
#     orchestrates those through the Slack MCP layer.
#   - The bot-user invite preflight implementation — that's procedural
#     in SKILL.md, not a mem-fusion primitive.
#   - Per-message progress logs at ~/.local/share/mem-fusion/logs/ —
#     deferred to a v0.5.x operational-polish pass.
#
# Idempotent. Verified by tests/mem_fusion/test-connector-pull-orchestration.py
# (14 checks across 7 cases — real peer envelope stored, re-ingest duplicate,
# non-envelope skipped, G11 default + override, loopback, integrity_failed,
# config/argument validation).
#
# Run via: bash 0.5.0-017-remember-pull-orchestration.sh

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

echo "==> Fix 0.5.0-017: /remember pull <connector-id> orchestration primitive"

# Pre-flight
[[ -f "$CORE_PY" ]]        || { log "ERROR: $CORE_PY not found"; exit 1; }
[[ -f "$MEM_FUSION_PY" ]]  || { log "ERROR: $MEM_FUSION_PY not found"; exit 1; }
[[ -x "$VENV_PY" ]]        || { log "ERROR: $VENV_PY not found"; exit 1; }
[[ -f "$SRC_CORE" ]]       || { log "ERROR: $SRC_CORE not found — run from a mem-fusion repo checkout"; exit 1; }
[[ -f "$SRC_MEM_FUSION" ]] || { log "ERROR: $SRC_MEM_FUSION not found"; exit 1; }
[[ -f "$SRC_SKILL" ]]      || { log "ERROR: $SRC_SKILL not found"; exit 1; }

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix A — deploy patched core.py (ingest_connector_message)
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── A: deploy patched core.py (ingest_connector_message) ──"
if grep -q '^async def ingest_connector_message' "$CORE_PY"; then
  log "  Already applied: ingest_connector_message present in deployed core.py."
else
  BACKUP="${CORE_PY}.bak.0.5.0-017"
  log "  Backing up to $BACKUP"
  cp "$CORE_PY" "$BACKUP"
  log "  Deploying canonical $SRC_CORE → $CORE_PY"
  cp "$SRC_CORE" "$CORE_PY"
  "$VENV_PY" -c "import ast; ast.parse(open('$CORE_PY').read())" \
    && log "  syntax ok" \
    || { log "  ERROR: deployed core.py failed to parse — restoring backup"; cp "$BACKUP" "$CORE_PY"; exit 1; }
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix B — deploy patched mem_fusion.py (ingest_connector_message tool)
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── B: deploy patched mem_fusion.py (ingest_connector_message MCP tool) ──"
if grep -q 'name="ingest_connector_message"' "$MEM_FUSION_PY"; then
  log "  Already applied: ingest_connector_message tool present in deployed mem_fusion.py."
else
  BACKUP="${MEM_FUSION_PY}.bak.0.5.0-017"
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
    BACKUP="${LOCAL_SKILL}.bak.0.5.0-017"
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
log "── verify: ingest_connector_message resolves via deployed core.py ──"
"$VENV_PY" <<'PY'
import os, sys, inspect
sys.path.insert(0, os.path.expanduser("~/.local/share/mem-fusion"))
import core

fn = getattr(core, "ingest_connector_message", None)
ok = fn is not None and inspect.iscoroutinefunction(fn)
print("  ✓ ingest_connector_message present + async" if ok
      else "  ✗ ingest_connector_message missing or not async")
sys.exit(0 if ok else 1)
PY

log ""
log "==> 0.5.0-017 complete. Pull-orchestration primitive deployed."
log "    New MCP tool (active on next Claude session): ingest_connector_message."
log "    SKILL.md procedural section guides /remember pull <connector-id>."
log "    G11 receive-side smoke-test filter active by default."
