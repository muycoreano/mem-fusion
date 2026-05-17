#!/usr/bin/env bash
# Fix 0.5.0-007 — embed() error attribution + input-length guard
#
# Bug report: 2026-05-16 by Shane Pitts in #branch-ai-mem-fusion.
# Mem-fusion memory id: 9ac9e015-58e3-4f1b-87d2-5bccdb962868
#
# TWO BUGS BUNDLED:
#   1. Error attribution wrong.
#      core.py:embed() returns None for every failure mode (connection
#      refused, HTTP 500 from oversized input, malformed response, etc.).
#      Callers then surface "ollama_unreachable" regardless of root cause.
#      Users chasing the wrong diagnostic (restart Ollama, restart Qdrant)
#      for what's actually an input-size issue.
#
#   2. Input-length guard missing.
#      No truncation or pre-validation of content length before sending to
#      Ollama. Predictably fails on memories >~8000 chars: nomic-embed-text
#      has a ~2048-token context window, and Ollama returns HTTP 500
#      ("llm embedding error: the input length exceeds the context length").
#
# THIS FIX:
#   1. Rewrites embed() to return either list[float] (success) OR a dict
#      with classified error: "ollama_unreachable" (genuine connection
#      failure), "embed_failed" (Ollama returned non-200, e.g. for oversized
#      input), "embed_unexpected" (malformed response / other failure).
#   2. Adds EMBED_MAX_CHARS=8000 soft-truncation before sending to Ollama.
#      Full content is preserved in the stored payload; only the embedding
#      is derived from the truncated text. Search-quality impact is minimal
#      (first 8000 chars typically capture the gist).
#   3. Updates 4 callers (store_memory, search_memory, upsert_memory,
#      find_or_create) to propagate dict-shaped errors verbatim instead of
#      hardcoding "ollama_unreachable".
#
# IDEMPOTENT: re-running after the fix is applied detects clean state and
# exits 0. The marker is the EMBED_MAX_CHARS constant.
#
# NO DAEMON RESTART required. The mem-fusion MCP server is spawned by Claude
# per-connection (stdio); the next session picks up the patched core.py.
#
# Run via: bash 0.5.0-007-fix-embed-error-attribution.sh

set -euo pipefail

MEMFUSION="${HOME}/.local/share/mem-fusion"
CORE_PY="${MEMFUSION}/core.py"
VENV_PY="${MEMFUSION}/venv/bin/python"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-007: embed() error attribution + input-length guard"

# 1. Pre-flight
[[ -f "$CORE_PY" ]] || { log "ERROR: $CORE_PY not found"; exit 1; }
[[ -x "$VENV_PY" ]] || { log "ERROR: $VENV_PY not found (mem-fusion venv missing?)"; exit 1; }

# 2. Idempotency check — EMBED_MAX_CHARS only exists post-fix.
if grep -q "^EMBED_MAX_CHARS" "$CORE_PY"; then
  log "Already fixed: EMBED_MAX_CHARS present in $CORE_PY."
  exit 0
fi

# 3. Backup deployed core.py
BACKUP="${CORE_PY}.bak.0.5.0-007"
log "Backing up to $BACKUP"
cp "$CORE_PY" "$BACKUP"

# 4. Apply patch via the venv's Python (string-replacement with ast.parse verify).
#    Each replacement must match exactly once; mismatch aborts the patch.
log "Patching $CORE_PY"
"$VENV_PY" - "$CORE_PY" <<'PYEOF'
import sys, ast

path = sys.argv[1]
with open(path) as f:
    src = f.read()

# ── Replacement 1: module docstring ──────────────────────────────────────
OLD_1 = '''If Ollama is unreachable when embedding is required, returns
{"error": "ollama_unreachable", "detail": ...}. Callers surface the error.
No silent queueing.
"""'''

NEW_1 = '''If Ollama is unreachable when embedding is required, returns
{"error": "ollama_unreachable", "detail": ...}. If Ollama is reachable but
rejects the input (e.g., HTTP 500 because the content exceeds the model's
context window), returns {"error": "embed_failed", "reason": ...}. Other
unexpected failures return {"error": "embed_unexpected", ...}. Callers
surface the error verbatim. No silent queueing.

Inputs exceeding EMBED_MAX_CHARS are soft-truncated before embedding; the
caller's stored payload retains the full content, only the vector is
derived from the truncated text.
"""'''

# ── Replacement 2: add EMBED_MAX_CHARS constant ──────────────────────────
OLD_2 = '''EMBED_MODEL = "nomic-embed-text"
VECTOR_SIZE = 768'''

NEW_2 = '''EMBED_MODEL = "nomic-embed-text"
VECTOR_SIZE = 768
# nomic-embed-text has a ~2048-token context window. ~8000 chars is the
# practical char-equivalent ceiling; inputs beyond this are soft-truncated
# before embedding (the full content is preserved in the stored payload).
EMBED_MAX_CHARS = 8000'''

# ── Replacement 3: rewrite embed() ───────────────────────────────────────
#    Pattern starts at function signature to avoid cosmetic header
#    underline character-count drift.
OLD_3 = '''async def embed(text: str) -> list[float] | None:
    """Generate a 768-dim vector via local Ollama. Returns None on failure."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/embeddings",
                                  json={"model": EMBED_MODEL, "prompt": text})
            r.raise_for_status()
            return r.json()["embedding"]
    except Exception as e:
        log.error("embed failed: %s", e)
        return None'''

NEW_3 = '''async def embed(text: str) -> list[float] | dict:
    """Generate a 768-dim vector via local Ollama.

    Returns either:
      list[float]                          — embedding on success
      {"error": "ollama_unreachable",  …}  — connection failure (Ollama down)
      {"error": "embed_failed",        …}  — Ollama returned non-200 (e.g., 500
                                             for input exceeding context length)
      {"error": "embed_unexpected",    …}  — malformed response or other failure

    Soft-truncates text to EMBED_MAX_CHARS before sending. Caller preserves
    the full content in storage; only the embedding is derived from the
    truncated text.
    """
    embed_text = text[:EMBED_MAX_CHARS] if len(text) > EMBED_MAX_CHARS else text
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/embeddings",
                                  json={"model": EMBED_MODEL, "prompt": embed_text})
            if r.status_code != 200:
                try:
                    reason = r.json().get("error") or r.text[:200]
                except Exception:
                    reason = r.text[:200]
                log.error("embed failed: HTTP %d — %s", r.status_code, reason)
                return {"error": "embed_failed",
                        "detail": f"Ollama returned HTTP {r.status_code}",
                        "reason": reason or "unknown",
                        "status_code": r.status_code}
            data = r.json()
            if "embedding" not in data:
                log.error("embed: unexpected response shape: %s", str(data)[:200])
                return {"error": "embed_unexpected",
                        "detail": "Ollama response missing 'embedding' field",
                        "reason": str(data)[:200]}
            return data["embedding"]
    except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
        log.error("embed: ollama unreachable — %s", e)
        return {"error": "ollama_unreachable",
                "detail": f"Cannot reach Ollama at {OLLAMA_URL}",
                "reason": str(e)}
    except Exception as e:
        log.error("embed: unexpected error — %s", e)
        return {"error": "embed_unexpected",
                "detail": "Unexpected error during embedding",
                "reason": str(e)}'''

# ── Replacement 4: store_memory docstring tail ───────────────────────────
OLD_4 = '''       {status: "duplicate", id, groups} — content_hash hit; nothing changed
       {error: "ollama_unreachable"}     — embed failed; caller surfaces error
    """'''

NEW_4 = '''       {status: "duplicate", id, groups} — content_hash hit; nothing changed
       {error: <class>, detail, ...}     — embed failed; caller surfaces error
                                           (class is one of ollama_unreachable,
                                           embed_failed, embed_unexpected)
    """'''

# ── Replacement 5: store_memory caller ───────────────────────────────────
OLD_5 = '''    vec = await embed(content)
    if vec is None:
        return {"error": "ollama_unreachable",
                "detail": "Local Ollama did not respond; cannot embed. Verify Ollama is running."}'''

NEW_5 = '''    vec = await embed(content)
    if isinstance(vec, dict):
        return vec  # error from embed() — propagate verbatim (preserves error class)'''

# ── Replacement 6: search_memory caller ──────────────────────────────────
OLD_6 = '''    vec = await embed(query)
    if vec is None:
        return {"error": "ollama_unreachable",
                "detail": "Local Ollama did not respond; cannot perform semantic search."}'''

NEW_6 = '''    vec = await embed(query)
    if isinstance(vec, dict):
        return vec  # error from embed() — propagate verbatim'''

# ── Replacement 7: upsert_memory caller (the Shane bug case) ─────────────
OLD_7 = '''    vec = await embed(content)
    if vec is None:
        return {"error": "ollama_unreachable",
                "detail": "Local Ollama did not respond; cannot re-embed for upsert."}'''

NEW_7 = '''    vec = await embed(content)
    if isinstance(vec, dict):
        return vec  # error from embed() — propagate verbatim'''

# ── Replacement 8: find_or_create caller ─────────────────────────────────
OLD_8 = '''    vec = await embed(content)
    if vec is None:
        return {"error": "ollama_unreachable",
                "detail": "Local Ollama did not respond; cannot embed for find_or_create."}'''

NEW_8 = '''    vec = await embed(content)
    if isinstance(vec, dict):
        return vec  # error from embed() — propagate verbatim'''

replacements = [
    ("module docstring",        OLD_1, NEW_1),
    ("EMBED_MAX_CHARS const",   OLD_2, NEW_2),
    ("embed() function body",   OLD_3, NEW_3),
    ("store_memory docstring",  OLD_4, NEW_4),
    ("store_memory caller",     OLD_5, NEW_5),
    ("search_memory caller",    OLD_6, NEW_6),
    ("upsert_memory caller",    OLD_7, NEW_7),
    ("find_or_create caller",   OLD_8, NEW_8),
]

new_src = src
for label, old, new in replacements:
    count = new_src.count(old)
    if count == 0:
        # Reverse-check: if the new text is already present, skip this rep.
        if new_src.count(new) >= 1:
            print(f"  [{label}] already patched, skipping")
            continue
        print(f"ERROR: pattern not found for '{label}' — file may have drifted from baseline",
              file=sys.stderr)
        sys.exit(1)
    if count != 1:
        print(f"ERROR: expected 1 occurrence of '{label}', got {count}", file=sys.stderr)
        sys.exit(1)
    new_src = new_src.replace(old, new, 1)
    print(f"  [{label}] applied")

# Verify syntax
try:
    ast.parse(new_src)
except SyntaxError as e:
    print(f"ERROR: patched file is not valid Python: {e}", file=sys.stderr)
    sys.exit(1)

with open(path, "w") as f:
    f.write(new_src)

print("  syntax ok, patched file written")
PYEOF

# 5. Final verification — import the patched module and check the new symbols exist.
log "Verifying patched module imports cleanly"
"$VENV_PY" - "$MEMFUSION" <<'PYEOF'
import sys, importlib.util
mf_dir = sys.argv[1]
sys.path.insert(0, mf_dir)
spec = importlib.util.spec_from_file_location("core", f"{mf_dir}/core.py")
mod  = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert hasattr(mod, "EMBED_MAX_CHARS"),     "EMBED_MAX_CHARS missing"
assert mod.EMBED_MAX_CHARS == 8000,         "EMBED_MAX_CHARS != 8000"
assert callable(mod.embed),                 "embed() missing"
print("  EMBED_MAX_CHARS = 8000 ✓")
print("  embed() callable ✓")
PYEOF

log "Fix 0.5.0-007 complete."
log "  backup: $BACKUP"
log "  effect: next mem-fusion MCP connection picks up the patched core.py"
log ""
log "Verification steps from the bug report (run by hand):"
log "  1. Try to /remember a memory with content > 8000 chars."
log "     Should succeed (embedding silently truncated; full content stored)."
log "  2. Stop Ollama: launchctl unload ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist"
log "     Try to /remember anything. Should return error class 'ollama_unreachable'."
log "  3. Restart Ollama: launchctl load ~/Library/LaunchAgents/com.branchapp.memfusion.ollama.plist"
log "     Same /remember succeeds."
exit 0
