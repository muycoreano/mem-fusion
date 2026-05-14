#!/usr/bin/env bash
#
# test-installers.sh — staleness + bash-syntax check for the install docs.
#
# Two invariants:
#   1. The committed INSTALL_*.md at repo root are byte-identical to a fresh
#      build from src/templates/*.tmpl. Detects "edited a template but forgot
#      to run ./build_install.sh".
#   2. Every ```bash code block in the generated docs parses with `bash -n`.
#      Catches busted shell (missing fi/done, unbalanced quotes, malformed
#      pipelines) without false-positiving on the embedded Python heredocs.
#
# Does NOT verify content correctness (e.g., that the CLAUDE.md snippet lists
# the right MCP tool count). That's deferred to a separate content-invariant
# pass if drift in that dimension surfaces.
#
# Usage:
#   tests/installer/test-installers.sh
#
# Exit code: 0 on all checks pass, 1 on any failure.

set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

passed=0
failed=0
fail_log="$TMP/failures.log"

check_pass() { echo "  ✓ $1"; passed=$((passed + 1)); }
check_fail() { echo "  ✗ $1"; printf '%s\n' "$1" >> "$fail_log"; failed=$((failed + 1)); }

echo "======================================================================"
echo "Installer build pipeline — staleness + bash-syntax check"
echo "======================================================================"

# ── STEP 1 — build into a tempdir (don't touch the working tree) ──────────
echo ""
echo "STEP 1: build INSTALL files into $TMP"
if ! "$ROOT/build_install.sh" "$TMP" > "$TMP/build.log" 2>&1; then
    check_fail "build_install.sh exited non-zero"
    sed 's/^/    /' "$TMP/build.log"
    echo ""
    echo "  $passed passed, $failed failed"
    exit 1
fi
if [[ -f "$TMP/INSTALL_MEM_FUSION.md" && -f "$TMP/INSTALL_CONSTELLATION.md" ]]; then
    check_pass "build script produced both INSTALL files"
else
    check_fail "build script did not produce both INSTALL files"
    exit 1
fi

# ── STEP 2 — staleness check: built == committed ──────────────────────────
echo ""
echo "STEP 2: built docs match committed INSTALL_*.md (staleness check)"
for f in INSTALL_MEM_FUSION.md INSTALL_CONSTELLATION.md; do
    if [[ ! -f "$ROOT/$f" ]]; then
        check_fail "$f missing from repo root"
        continue
    fi
    if diff -q "$ROOT/$f" "$TMP/$f" > /dev/null 2>&1; then
        check_pass "$f in sync with templates"
    else
        delta="$(diff "$ROOT/$f" "$TMP/$f" | wc -l | tr -d ' ')"
        check_fail "$f drifted from templates ($delta diff-line delta) — run ./build_install.sh and commit"
    fi
done

# ── STEP 3 — bash-syntax check on every ```bash block ─────────────────────
echo ""
echo "STEP 3: bash code blocks in generated docs parse cleanly (bash -n)"
for doc in INSTALL_MEM_FUSION.md INSTALL_CONSTELLATION.md; do
    n_blocks=0
    n_failed=0
    failed_blocks=""
    in_block=0
    block_idx=0
    block_path="$TMP/block.sh"
    : > "$block_path"
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == '```bash' ]]; then
            in_block=1
            block_idx=$((block_idx + 1))
            : > "$block_path"
        elif [[ "$line" == '```' && "$in_block" == 1 ]]; then
            in_block=0
            n_blocks=$((n_blocks + 1))
            if ! err="$(bash -n "$block_path" 2>&1)"; then
                n_failed=$((n_failed + 1))
                failed_blocks="${failed_blocks}    block #$block_idx: $err"$'\n'
            fi
        elif [[ "$in_block" == 1 ]]; then
            printf '%s\n' "$line" >> "$block_path"
        fi
    done < "$TMP/$doc"
    if [[ "$n_failed" == 0 ]]; then
        check_pass "$doc: $n_blocks bash blocks parse"
    else
        check_fail "$doc: $n_failed/$n_blocks bash blocks failed bash -n"
        printf '%s' "$failed_blocks"
    fi
done

echo ""
echo "  $passed passed, $failed failed"
echo ""
if [[ "$failed" -gt 0 ]]; then
    echo "======================================================================"
    echo "✗ installer build pipeline FAILED"
    echo "======================================================================"
    exit 1
fi
echo "======================================================================"
echo "✓ installer build pipeline PASSED"
echo "======================================================================"
