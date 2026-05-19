#!/usr/bin/env bash
# mem-fusion install.sh — self-bootstrapping, idempotent, 16-step installer.
#
# Spec: docs/v0.5_INSTALL_FLOW.md (architect-authored, 2026-05-18).
#
# Three invocation paths, one script:
#   1. curl-pipe:       curl -fsSL .../install.sh | bash
#   2. Claude-mediated: user asks Claude to install; Claude runs the same curl line
#   3. Dev mode:        git clone && cd && bash install.sh
#
# Idempotent end-to-end: fresh install, partial upgrade, fully-current install
# all run the same command with correct behavior for each.
#
# Pure shell + small Python helpers. No Claude in the critical path.
# Slack connector setup lives in a skill loaded at step 13, NOT in this script
# (Slack MCP auth lives in Claude's process).

set -euo pipefail

# ────────────────────────────────────────────────────────────────────────────
# Step 1 — Self-bootstrap
# ────────────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null)" || SCRIPT_DIR=""
# Adjacency check: detection key is a file that exists in every clone of the repo.
if [ -z "$SCRIPT_DIR" ] || [ ! -f "${SCRIPT_DIR}/src/scripts/fixes/0.5.0-001-fix-qdrant-home-path.sh" ]; then
    CLONE_DIR="${MEMFUSION_CLONE_DIR:-${HOME}/dev/mem-fusion}"
    REPO_URL="${MEMFUSION_REPO_URL:-https://github.com/muycoreano/mem-fusion}"
    BRANCH="${MEMFUSION_BRANCH:-v0.5}"
    echo "==> Self-bootstrap: cloning/updating ${REPO_URL} → ${CLONE_DIR}"
    if [ ! -d "${CLONE_DIR}/.git" ]; then
        git clone "${REPO_URL}" "${CLONE_DIR}"
    else
        (cd "${CLONE_DIR}" && git fetch origin && git checkout "${BRANCH}" && git pull --ff-only)
    fi
    cd "${CLONE_DIR}"
    exec bash install.sh "$@"
fi
cd "${SCRIPT_DIR}"

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────
HOME_LIT="${HOME}"
MEMFUSION="${HOME_LIT}/.local/share/mem-fusion"
VENV_PY="${MEMFUSION}/venv/bin/python"
QDRANT_BIN="${MEMFUSION}/bin/qdrant"
QDRANT_VER="${MEMFUSION_QDRANT_VER:-1.13.4}"
QDRANT_URL="http://127.0.0.1:6333"
OLLAMA_URL="http://127.0.0.1:11434"
LAUNCHD_DIR="${HOME_LIT}/Library/LaunchAgents"
ARCH="$(uname -m)"
SETTINGS_PATH="${HOME_LIT}/.claude/settings.json"
CLAUDE_MD="${HOME_LIT}/CLAUDE.md"
CLAUDE_SKILLS_DIR="${HOME_LIT}/.claude/skills"

# Telemetry counters for the final summary.
PRELOAD_SCANNED=0
FIX_TOTAL=0
FIX_APPLIED=0
FIX_SKIPPED=0
FIX_FAILED=0

log()  { printf "  %s\n" "$*"; }
step() { printf "\n==> Step %s — %s\n" "$1" "$2"; }

# ────────────────────────────────────────────────────────────────────────────
# Step 2 — Pre-flight
# ────────────────────────────────────────────────────────────────────────────
step 2 "Pre-flight checks"

[[ "$(uname)" == "Darwin" ]] || { log "ERROR: macOS-only installer"; exit 1; }
log "  uname: Darwin / ${ARCH}"

if [[ "${ARCH}" == "arm64" ]]; then
    BREW_PREFIX="/opt/homebrew"
else
    BREW_PREFIX="/usr/local"
fi
[[ -x "${BREW_PREFIX}/bin/brew" ]] || {
    log "ERROR: Homebrew not found at ${BREW_PREFIX}/bin/brew. Install: https://brew.sh"
    exit 1
}
log "  brew:  ${BREW_PREFIX}/bin/brew"

command -v claude >/dev/null || {
    log "ERROR: Claude Code CLI not found on PATH. Install: https://claude.ai/download"
    exit 1
}
log "  claude: $(command -v claude)"

# Port collisions — informational, not fatal (idempotent re-runs hit these legitimately).
if lsof -nP -iTCP:6333 -sTCP:LISTEN -t >/dev/null 2>&1; then
    log "  port 6333: in use (Qdrant — will be reused if it's ours)"
fi
if lsof -nP -iTCP:11434 -sTCP:LISTEN -t >/dev/null 2>&1; then
    log "  port 11434: in use (Ollama — will be reused)"
fi

# Existing-install detection — informational; idempotent steps handle the rest.
if [[ -x "${QDRANT_BIN}" ]] && curl -sf "${QDRANT_URL}/healthz" >/dev/null 2>&1 \
   && claude mcp list 2>/dev/null | grep -q "mem-fusion"; then
    log "  existing install detected — will upgrade in place"
else
    log "  fresh install (no Qdrant or no MCP registration found)"
fi

# ────────────────────────────────────────────────────────────────────────────
# Step 3 — Homebrew deps
# ────────────────────────────────────────────────────────────────────────────
step 3 "Homebrew deps (python@3.12, ollama)"
if ! "${BREW_PREFIX}/bin/brew" list --formula python@3.12 >/dev/null 2>&1; then
    log "  installing python@3.12..."
    "${BREW_PREFIX}/bin/brew" install python@3.12
else
    log "  python@3.12: already installed"
fi
if ! "${BREW_PREFIX}/bin/brew" list --formula ollama >/dev/null 2>&1; then
    log "  installing ollama..."
    "${BREW_PREFIX}/bin/brew" install ollama
else
    log "  ollama: already installed"
fi
PYTHON312="${BREW_PREFIX}/bin/python3.12"
OLLAMA_BIN="${BREW_PREFIX}/bin/ollama"

# ────────────────────────────────────────────────────────────────────────────
# Step 4 — Directory tree
# ────────────────────────────────────────────────────────────────────────────
step 4 "Directory tree under ${MEMFUSION}"
mkdir -p "${MEMFUSION}"/{bin,scripts,logs,queue,qdrant-data,snapshots}
mkdir -p "${LAUNCHD_DIR}"
mkdir -p "${CLAUDE_SKILLS_DIR}"
log "  ok"

# ────────────────────────────────────────────────────────────────────────────
# Step 5 — Python venv + requirements
# ────────────────────────────────────────────────────────────────────────────
step 5 "Python venv + requirements"
if [[ ! -x "${VENV_PY}" ]]; then
    log "  creating venv with ${PYTHON312}..."
    "${PYTHON312}" -m venv "${MEMFUSION}/venv"
    "${VENV_PY}" -m pip install --upgrade pip >/dev/null
fi
# Pin versions match the existing INSTALL doc + ship-tested baseline.
REQS="mcp==1.6.0 qdrant-client==1.13.1 httpx==0.28.1"
NEED_INSTALL=0
for pkg in $REQS; do
    name="${pkg%%==*}"
    ver="${pkg##*==}"
    have="$("${VENV_PY}" -m pip show "${name}" 2>/dev/null | awk '/^Version:/ {print $2}')"
    if [[ "${have}" != "${ver}" ]]; then NEED_INSTALL=1; break; fi
done
if [[ "${NEED_INSTALL}" == "1" ]]; then
    log "  installing/refreshing pinned deps..."
    "${VENV_PY}" -m pip install --quiet ${REQS}
fi
cat > "${MEMFUSION}/requirements.txt" <<EOF
mcp==1.6.0
qdrant-client==1.13.1
httpx==0.28.1
EOF
log "  ok"

# ────────────────────────────────────────────────────────────────────────────
# Step 6 — Qdrant binary
# ────────────────────────────────────────────────────────────────────────────
step 6 "Qdrant binary v${QDRANT_VER}"
NEED_QDRANT=1
if [[ -x "${QDRANT_BIN}" ]]; then
    if "${QDRANT_BIN}" --version 2>/dev/null | grep -q "${QDRANT_VER}"; then
        log "  qdrant v${QDRANT_VER}: already present"
        NEED_QDRANT=0
    fi
fi
if [[ "${NEED_QDRANT}" == "1" ]]; then
    if [[ "${ARCH}" == "arm64" ]]; then
        ASSET="qdrant-aarch64-apple-darwin.tar.gz"
    else
        ASSET="qdrant-x86_64-apple-darwin.tar.gz"
    fi
    log "  downloading ${ASSET}..."
    curl -fsSL -o /tmp/qdrant-mf.tar.gz \
        "https://github.com/qdrant/qdrant/releases/download/v${QDRANT_VER}/${ASSET}"
    tar -xzf /tmp/qdrant-mf.tar.gz -C /tmp
    mv -f /tmp/qdrant "${QDRANT_BIN}"
    chmod +x "${QDRANT_BIN}"
    rm -f /tmp/qdrant-mf.tar.gz
    log "  installed $("${QDRANT_BIN}" --version 2>/dev/null | head -1)"
fi

# ────────────────────────────────────────────────────────────────────────────
# Step 7 — Qdrant config (absolute paths — no $HOME-in-yaml bug)
# ────────────────────────────────────────────────────────────────────────────
step 7 "Qdrant config (absolute paths)"
cat > "${MEMFUSION}/qdrant-config.yaml" <<EOF
storage:
  storage_path: ${HOME_LIT}/.local/share/mem-fusion/qdrant-data

snapshots_config:
  snapshots_path: ${HOME_LIT}/.local/share/mem-fusion/qdrant-data/snapshots

service:
  host: 127.0.0.1
  http_port: 6333
  grpc_port: 6334
  enable_cors: false

log_level: WARN
EOF
log "  written: ${MEMFUSION}/qdrant-config.yaml"

# ────────────────────────────────────────────────────────────────────────────
# Step 8 — Deploy source (cp with content-hash skip)
# ────────────────────────────────────────────────────────────────────────────
step 8 "Deploy source (core.py, mem_fusion.py, constellation.py, connectors/, scripts/)"

# Single files (top-level).
for fname in core.py mem_fusion.py constellation.py; do
    src="${SCRIPT_DIR}/src/${fname}"
    dst="${MEMFUSION}/${fname}"
    if [[ -f "${dst}" ]] && cmp -s "${src}" "${dst}"; then
        log "  unchanged: ${fname}"
    else
        if [[ -f "${dst}" ]]; then
            cp "${dst}" "${dst}.bak.install-$(date +%Y%m%d-%H%M%S)"
        fi
        cp "${src}" "${dst}"
        log "  deployed: ${fname}"
    fi
done

# connectors/ package — recursive copy.
mkdir -p "${MEMFUSION}/connectors"
DEPLOYED_CONNECTORS=0
for src in "${SCRIPT_DIR}"/src/connectors/*.py; do
    fname="$(basename "${src}")"
    dst="${MEMFUSION}/connectors/${fname}"
    if [[ -f "${dst}" ]] && cmp -s "${src}" "${dst}"; then continue; fi
    cp "${src}" "${dst}"
    DEPLOYED_CONNECTORS=$((DEPLOYED_CONNECTORS + 1))
done
log "  connectors/: ${DEPLOYED_CONNECTORS} updated (others unchanged)"

# scripts/ — top-level helpers + hooks.
DEPLOYED_SCRIPTS=0
for src in "${SCRIPT_DIR}"/src/scripts/*.py "${SCRIPT_DIR}"/src/scripts/*.sh; do
    [[ -f "${src}" ]] || continue
    fname="$(basename "${src}")"
    dst="${MEMFUSION}/scripts/${fname}"
    if [[ -f "${dst}" ]] && cmp -s "${src}" "${dst}"; then continue; fi
    cp "${src}" "${dst}"
    chmod +x "${dst}" 2>/dev/null || true
    DEPLOYED_SCRIPTS=$((DEPLOYED_SCRIPTS + 1))
done
log "  scripts/: ${DEPLOYED_SCRIPTS} updated"

# Constellation config skeleton + claude_md_snippet.
cp "${SCRIPT_DIR}/src/config/claude_md_snippet.md" "${MEMFUSION}/claude_md_snippet.md"
if [[ ! -f "${MEMFUSION}/constellation/config.json" ]]; then
    mkdir -p "${MEMFUSION}/constellation"
    if [[ -f "${SCRIPT_DIR}/src/config/constellation.example.json" ]]; then
        cp "${SCRIPT_DIR}/src/config/constellation.example.json" "${MEMFUSION}/constellation/config.json"
        log "  constellation/config.json: scaffolded from example"
    fi
fi

# NOTE: Qdrant collection init runs AFTER step 9 (launchd plist load) since it
# needs Qdrant up. We just deployed core.py / connectors; collection-init is
# downstream of daemon startup.

# ────────────────────────────────────────────────────────────────────────────
# Step 9 — launchd plists (Qdrant, Ollama; Constellation conditional)
# ────────────────────────────────────────────────────────────────────────────
step 9 "launchd plists"

# Render plist by expanding ${HOME_LIT} + ${OLLAMA_BIN} placeholders.
render_plist() {
    local src="$1"
    local dst="$2"
    sed -e "s|\${HOME_LIT}|${HOME_LIT}|g" \
        -e "s|\${OLLAMA_BIN}|${OLLAMA_BIN}|g" \
        "${src}" > "${dst}"
}

# Load-or-refresh a launchd plist. Suppresses launchctl's stray-PID echo.
#
# Idempotency-preserving: if the service is already loaded AND its underlying
# HTTP surface is healthy, this is a no-op. The bootout/bootstrap dance is
# destructive on warm systems (it temporarily removes the service from the
# launchd domain, and recovery can take >30s) — so we only run it when the
# service is actually broken or missing.
load_plist() {
    local plist="$1"
    local label="$2"
    local health_url="${3:-}"

    # Fast path: service loaded AND health URL responsive → leave alone.
    if [[ -n "${health_url}" ]] \
       && launchctl list 2>/dev/null | grep -q "${label}" \
       && curl -sf --max-time 2 "${health_url}" >/dev/null 2>&1; then
        return 0
    fi

    # Cold path: service is loaded but broken, OR not loaded. Refresh.
    if launchctl list 2>/dev/null | grep -q "${label}"; then
        launchctl bootout "gui/${UID}/${label}" >/dev/null 2>&1 || true
    fi
    launchctl bootstrap "gui/${UID}" "${plist}" >/dev/null 2>&1 \
        || launchctl load "${plist}" >/dev/null 2>&1 || true
    launchctl kickstart -p "gui/${UID}/${label}" >/dev/null 2>&1 || true
}

# Pair each daemon's label with its health URL so load_plist can skip the
# destructive bootout/bootstrap dance when the daemon is already healthy.
declare -A DAEMON_HEALTH=(
    ["com.branchapp.memfusion.qdrant"]="${QDRANT_URL}/healthz"
    ["com.branchapp.memfusion.ollama"]="${OLLAMA_URL}/api/version"
)
for plist_name in com.branchapp.memfusion.qdrant.plist com.branchapp.memfusion.ollama.plist; do
    src="${SCRIPT_DIR}/src/launchd/${plist_name}"
    dst="${LAUNCHD_DIR}/${plist_name}"
    render_plist "${src}" "${dst}"
    label="${plist_name%.plist}"
    health_url="${DAEMON_HEALTH[${label}]:-}"
    if load_plist "${dst}" "${label}" "${health_url}"; then
        if [[ -n "${health_url}" ]] && curl -sf --max-time 2 "${health_url}" >/dev/null 2>&1; then
            log "  ${label}: already healthy (no restart)"
        else
            log "  ${label}: loaded + kicked"
        fi
    fi
done

# Constellation plist is conditional on the user's config — install but only
# load if config.json exists with at least one configured group.
CONSTELL_PLIST="${LAUNCHD_DIR}/com.branchapp.memfusion.constellation.plist"
render_plist "${SCRIPT_DIR}/src/launchd/com.branchapp.memfusion.constellation.plist" "${CONSTELL_PLIST}"
if [[ -f "${MEMFUSION}/constellation/config.json" ]] && grep -q '"groups"' "${MEMFUSION}/constellation/config.json"; then
    load_plist "${CONSTELL_PLIST}" "com.branchapp.memfusion.constellation"
    log "  com.branchapp.memfusion.constellation: loaded + kicked (groups configured)"
else
    log "  constellation: plist installed; daemon not started (no groups configured)"
fi

# Health-check loop — give Qdrant + Ollama time to come up. launchd's RunAtLoad
# is sometimes flaky after bootout/bootstrap; explicit kickstart at the 10s mark
# guarantees the daemons get a start signal even if RunAtLoad didn't fire. The
# 60s ceiling accommodates cold-start latencies on slower or warm-but-recently-
# bounced systems where the bootout/bootstrap dance needs longer to settle.
log "  waiting for Qdrant + Ollama (up to 60s)..."
DAEMONS_UP=0
for i in $(seq 1 60); do
    if curl -sf --max-time 2 "${QDRANT_URL}/healthz" >/dev/null 2>&1 \
       && curl -sf --max-time 2 "${OLLAMA_URL}/api/version" >/dev/null 2>&1; then
        log "  both up after ~${i}s"
        DAEMONS_UP=1
        break
    fi
    if [[ ${i} -eq 10 ]]; then
        launchctl kickstart -p "gui/${UID}/com.branchapp.memfusion.qdrant" >/dev/null 2>&1 || true
        launchctl kickstart -p "gui/${UID}/com.branchapp.memfusion.ollama" >/dev/null 2>&1 || true
    fi
    sleep 1
done
if [[ ${DAEMONS_UP} -eq 0 ]]; then
    log "  WARNING: daemons not yet healthy after 60s — downstream steps may degrade"
fi

# Pull the embedding model if missing.
if ! curl -sf "${OLLAMA_URL}/api/tags" 2>/dev/null | grep -q 'nomic-embed-text'; then
    log "  pulling nomic-embed-text model..."
    "${OLLAMA_BIN}" pull nomic-embed-text >/dev/null 2>&1 || log "  WARNING: model pull failed; re-run later"
else
    log "  nomic-embed-text: already pulled"
fi

# Init the Qdrant collection (idempotent). Needs Qdrant healthy → runs here.
log "  initializing Qdrant collection..."
"${VENV_PY}" "${MEMFUSION}/scripts/init_collection.py" >/dev/null 2>&1 || log "  WARNING: collection init failed; fix scripts may recover"

# ────────────────────────────────────────────────────────────────────────────
# Step 10 — claude mcp add mem-fusion (idempotent)
# ────────────────────────────────────────────────────────────────────────────
step 10 "Claude MCP registration"
if claude mcp list 2>/dev/null | grep -q "^mem-fusion:"; then
    log "  mem-fusion: already registered"
else
    claude mcp add mem-fusion -s user \
        "${VENV_PY}" "${MEMFUSION}/mem_fusion.py" >/dev/null
    log "  mem-fusion: registered (user scope)"
fi

# ────────────────────────────────────────────────────────────────────────────
# Step 11 — settings.json hooks (anchor-based; backup + refresh)
# ────────────────────────────────────────────────────────────────────────────
step 11 "settings.json hook entries"
# wire_hooks.py is idempotent and backs up before writing.
if "${VENV_PY}" "${MEMFUSION}/scripts/wire_hooks.py" >/dev/null; then
    log "  4 hooks wired into ${SETTINGS_PATH}"
fi

# ────────────────────────────────────────────────────────────────────────────
# Step 12 — CLAUDE.md Vector Memory section (anchor-based)
# ────────────────────────────────────────────────────────────────────────────
step 12 "CLAUDE.md Vector Memory section"
if "${VENV_PY}" "${MEMFUSION}/scripts/merge_claude_md.py"; then
    log "  CLAUDE.md updated (backup written if content changed)"
fi

# ────────────────────────────────────────────────────────────────────────────
# Step 13 — Skills install (~/.claude/skills/)
# ────────────────────────────────────────────────────────────────────────────
step 13 "Skills install (remember + mem-fusion-slack-connector)"

deploy_skill() {
    local name="$1"
    local src="${SCRIPT_DIR}/src/skills/${name}"
    local dst="${CLAUDE_SKILLS_DIR}/${name}"
    [[ -d "${src}" ]] || { log "  skipped: ${name} (no source dir)"; return; }
    mkdir -p "${dst}"
    local updated=0
    for f in "${src}"/*; do
        [[ -f "${f}" ]] || continue
        local fname="$(basename "${f}")"
        local d="${dst}/${fname}"
        if [[ -f "${d}" ]] && cmp -s "${f}" "${d}"; then continue; fi
        if [[ -f "${d}" ]]; then cp "${d}" "${d}.bak.install-$(date +%Y%m%d-%H%M%S)"; fi
        cp "${f}" "${d}"
        updated=$((updated + 1))
    done
    log "  ${name}: ${updated} files updated"
}

deploy_skill remember
deploy_skill mem-fusion-slack-connector

# ────────────────────────────────────────────────────────────────────────────
# Step 14 — Iterate fix scripts (numeric order; idempotent per-script)
# ────────────────────────────────────────────────────────────────────────────
step 14 "Iterate fix scripts (0.5.0-*)"

# Wait for Qdrant + Ollama up. Several fix scripts bounce them; ensure they're
# back before the next script touches them. Tries an explicit kickstart at
# the half-way mark in case launchd's RunAtLoad didn't fire reliably.
wait_for_daemons() {
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
        if curl -sf "${QDRANT_URL}/healthz" >/dev/null 2>&1 \
           && curl -sf "${OLLAMA_URL}/api/version" >/dev/null 2>&1; then return 0; fi
        if [[ ${i} -eq 10 ]]; then
            launchctl kickstart -p "gui/${UID}/com.branchapp.memfusion.qdrant" >/dev/null 2>&1 || true
            launchctl kickstart -p "gui/${UID}/com.branchapp.memfusion.ollama" >/dev/null 2>&1 || true
        fi
        sleep 1
    done
    return 1
}

# Idempotency-detect phrases observed across the v0.5 fix-script corpus.
# A script counts as "skipped" when its output contains any of these.
# The "[Aa]lready <verb>" idiom is the convention across all 14 fix scripts:
# applied, migrated, current, present, exists, fixed, patched, mirrored,
# deployed, canonical, clean, covered, in, has, have, supports. Matching
# the prefix-plus-any-lowercase-suffix keeps the regex future-proof — new
# fix scripts can use any "Already <verb>" phrasing without an installer edit.
IDEMPOTENT_PATTERNS='[Aa]lready [a-z]+|[Nn]o restart needed'

for fix in $(ls "${SCRIPT_DIR}"/src/scripts/fixes/0.5.0-*.sh 2>/dev/null | sort -V); do
    fname="$(basename "${fix}")"
    FIX_TOTAL=$((FIX_TOTAL + 1))

    # Best-effort daemon wait — if they're not up, let the fix script's own
    # pre-flight checks decide whether to skip. Cascade-skipping the entire
    # corpus here was the v0.5 ship-blocker we already fixed once; do not
    # reintroduce.
    wait_for_daemons || log "  (daemons not ready; ${fname} will self-check)"

    # Capture output + exit code. Fix scripts use set -e so a real failure
    # exits non-zero; we treat that as FAILED. Pure idempotency reports get
    # classified by pattern match.
    set +e
    out="$(bash "${fix}" 2>&1)"
    rc=$?
    set -e

    if [[ ${rc} -ne 0 ]]; then
        FIX_FAILED=$((FIX_FAILED + 1))
        log "  ${fname}: FAILED (exit ${rc})"
        printf '%s\n' "${out}" | tail -5 | sed -e 's/^/      /'
        continue
    fi

    if printf '%s' "${out}" | grep -qE "${IDEMPOTENT_PATTERNS}"; then
        FIX_SKIPPED=$((FIX_SKIPPED + 1))
        log "  ${fname}: skipped (already applied)"
    else
        FIX_APPLIED=$((FIX_APPLIED + 1))
        log "  ${fname}: applied"
    fi
done

# ────────────────────────────────────────────────────────────────────────────
# Step 15 — Preload + import memories
# ────────────────────────────────────────────────────────────────────────────
# These steps depend on Qdrant + Ollama. Use set +e so a transient daemon
# unavailability degrades to a warning, not a script-killing exit.
set +e
step 15 "Preload canonical memories + import file-based"

# Preload 8 canonical memories (idempotent via find_or_create).
if "${VENV_PY}" "${MEMFUSION}/scripts/preload_usage_memories.py" >/dev/null 2>&1; then
    log "  preload: 8 canonical memories ensured (duplicates no-op)"
else
    log "  preload: skipped (Qdrant/Ollama not ready)"
fi
# Import ~/.claude/projects/*/memory/*.md (one-time migration; idempotent on re-run).
import_out="$("${VENV_PY}" "${MEMFUSION}/scripts/import_local_memories.py" 2>&1 || true)"
PRELOAD_SCANNED="$(printf '%s' "${import_out}" | awk '/scanned|imported/ {print $NF; exit}')"
[[ -z "${PRELOAD_SCANNED}" ]] && PRELOAD_SCANNED="0"
log "  file-memory import: completed"

# ────────────────────────────────────────────────────────────────────────────
# Step 16 — Final summary + handoff
# ────────────────────────────────────────────────────────────────────────────
TOTAL_MEMS="$("${VENV_PY}" -c "
import sys
sys.path.insert(0, '${MEMFUSION}')
import core
print(core.qdrant.count(collection_name=core.COLLECTION, exact=True).count)
" 2>/dev/null)"
[[ -z "${TOTAL_MEMS}" ]] && TOTAL_MEMS="?"

CONSTELL_STATUS="not configured"
if curl -sf "http://127.0.0.1:7533/healthz" >/dev/null 2>&1 || curl -sf "http://127.0.0.1:7534/healthz" >/dev/null 2>&1; then
    CONSTELL_STATUS="running (127.0.0.1:7533/7534)"
fi

echo ""
echo "========================================"
echo "mem-fusion install complete"
echo ""
printf "  %-14s %s\n" "Qdrant:"        "running (127.0.0.1:6333)"
printf "  %-14s %s\n" "Ollama:"        "running (127.0.0.1:11434)"
printf "  %-14s %s\n" "Constellation:" "${CONSTELL_STATUS}"
printf "  %-14s %s\n" "MCP:"           "registered (mem-fusion)"
printf "  %-14s %s\n" "Hooks:"         "4 wired"
printf "  %-14s %s\n" "Skills:"        "2 installed (remember, mem-fusion-slack-connector)"
printf "  %-14s %s\n" "Memories:"      "${TOTAL_MEMS} stored, 8 canonical preloaded this run"
printf "  %-14s %s\n" "Fix scripts:"   "${FIX_TOTAL} total — ${FIX_APPLIED} applied, ${FIX_SKIPPED} skipped, ${FIX_FAILED} failed"
echo ""
echo "NEXT STEPS:"
echo "  1. Restart Claude Code (Ctrl-D / reopen) so the MCP picks up the new code."
echo "  2. (Optional) Ask Claude to set up the mem-fusion Slack connector"
echo "     for cross-machine memory sharing."
echo ""
printf "  Repo:   %s\n" "${SCRIPT_DIR}"
printf "  Logs:   %s/logs/\n" "${MEMFUSION}"
echo "========================================"

# Explicit success exit so any non-fatal warning during the cleanup steps
# (Step 15 import, Step 16 stats) doesn't poison the final return code.
# Real failures earlier in the script trip set -e and never reach here.
exit 0
