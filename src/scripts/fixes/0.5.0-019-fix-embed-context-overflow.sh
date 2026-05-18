#!/usr/bin/env bash
# Fix 0.5.0-019 - chunked embedding for long content
#
# RESOLVES: Marketing-flagged bug in memory 581c761d (2026-05-18) -
# storing a ~10000-char memory containing dense markdown + code blocks
# returned `embed_failed` HTTP 500 ("the input length exceeds the
# context length") DESPITE 0.5.0-007's EMBED_MAX_CHARS=8000 soft-
# truncation guard.
#
# WHY TRUNCATION ALONE WAS INSUFFICIENT
# nomic-embed-text's ~2048-token context can be exceeded at well under
# 8000 chars when content is markdown/code-dense (high tokens-per-char).
# A single threshold cannot serve all content types. Worse, single-vector
# truncation loses semantic signal past the truncation point - the deeper
# content is invisible to search.
#
# THE FIX: CHUNKED EMBEDDING
# Content above EMBED_CHUNK_THRESHOLD (4000 chars) is split into
# ~EMBED_CHUNK_CHARS (1500) overlapping windows with EMBED_CHUNK_OVERLAP
# (200) chars of overlap. Each chunk is embedded independently. Chunks
# share a canonical_id linking them; chunk 0 stores the full content +
# metadata while chunks 1..N-1 carry only their slice + the linkage.
# Search dedupes by canonical_id and returns the canonical chunk's full
# payload - so the whole memory stays searchable AND callers see one
# record per memory.
#
# DEFENSE IN DEPTH: embed() also retains an explicit context-overflow
# retry (detects "exceeds the context length" in Ollama's error and
# halves the truncation, down to EMBED_RETRY_MIN_CHARS=1000) so a
# pathological single chunk still embeds rather than failing the store.
#
# DEPLOYS:
#   - Patched src/core.py:
#       + EMBED_CHUNK_THRESHOLD / EMBED_CHUNK_CHARS / EMBED_CHUNK_OVERLAP
#       + EMBED_CONTEXT_OVERFLOW_NEEDLE / EMBED_RETRY_MIN_CHARS
#       + chunk_text() helper
#       + embed_chunks() helper (returns list[vec] per chunk)
#       + _embed_once() private helper (single Ollama call extracted from
#         embed for the retry loop)
#       + embed() rewritten as overflow-retry around _embed_once
#       + store_memory: chunked multi-point insert when content > threshold
#       + search_memory: over-fetch + dedup by canonical_id + canonical-
#         chunk payload resolution for non-canonical hits
#       + search_recent: filter out chunk_index != 0
#       + delete_memory: cascades to all chunks via canonical_id
#       + upsert_memory: re-chunks via store_memory when content >
#         threshold OR existing memory was chunked
#       + find_or_create: delegates to store_memory on novel content
#       + store_memory_from_envelope: chunked receive path; envelope.vector
#         used only when content fits a single chunk
#
# BACK-COMPAT: pre-0.5.0-019 memories have no canonical_id / chunk_index;
# treated as single chunks (canonical_id implied = id; chunk_index = 0).
# No payload migration required - the new schema layers cleanly on top
# of every existing point.
#
# Idempotent. Marker: presence of EMBED_CHUNK_THRESHOLD in deployed
# core.py. Re-runs are full no-ops.

set -euo pipefail

MEMFUSION="${HOME}/.local/share/mem-fusion"
CORE_PY="${MEMFUSION}/core.py"
VENV_PY="${MEMFUSION}/venv/bin/python"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SRC_CORE="${SRC_ROOT}/core.py"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-019: chunked embedding (Marketing bug 581c761d)"

[[ -f "$CORE_PY" ]]  || { log "ERROR: $CORE_PY not found"; exit 1; }
[[ -x "$VENV_PY" ]]  || { log "ERROR: $VENV_PY not found"; exit 1; }
[[ -f "$SRC_CORE" ]] || { log "ERROR: $SRC_CORE not found"; exit 1; }

log ""
log "-- A: deploy patched core.py (chunked embedding) --"
if grep -q "EMBED_CHUNK_THRESHOLD" "$CORE_PY"; then
    log "  Already applied: EMBED_CHUNK_THRESHOLD present in deployed core.py."
else
    BACKUP="${CORE_PY}.bak.0.5.0-019"
    log "  Backing up to $BACKUP"
    cp "$CORE_PY" "$BACKUP"
    log "  Deploying canonical $SRC_CORE -> $CORE_PY"
    cp "$SRC_CORE" "$CORE_PY"
    "$VENV_PY" -c "import ast; ast.parse(open('$CORE_PY').read())" \
        && log "  syntax ok" \
        || { log "  ERROR: deployed core.py failed to parse - restoring backup"; cp "$BACKUP" "$CORE_PY"; exit 1; }
fi

log ""
log "-- verify: 10K dense markdown stores as chunked and retrieves whole --"
"$VENV_PY" <<'PY'
import asyncio, os, sys, uuid
sys.path.insert(0, os.path.expanduser("~/.local/share/mem-fusion"))
import core

block = (
    "## H **b** `c` [l](https://x.com)\n"
    "```python\n"
    "def f(a, b=None):\n"
    "    return {'a': 1, 'b': [2,3,4]}.get(a)\n"
    "```\n"
    "- item with `code`\n"
)
content = ""
while len(content) < 10_000:
    content += block
content = content[:10_000]

async def main():
    project_tag = f"chunked-019-verify-{uuid.uuid4().hex[:8]}"
    r = await core.store_memory({"content": content, "type": "context", "project": project_tag})
    if r.get("status") != "stored" or r.get("chunk_count", 0) < 2:
        print(f"  - unexpected store result: {r}")
        sys.exit(1)
    canonical_id = r["id"]
    chunks = r["chunk_count"]
    print(f"  + 10K dense -> stored as {chunks} chunks (canonical_id={canonical_id[:8]})")

    # Search retrieves the canonical chunk
    sr = await core.search_memory({"query": "python function dict get", "project": project_tag, "top_k": 3})
    if not sr.get("results"):
        print("  - search returned 0 hits")
        sys.exit(1)
    top = sr["results"][0]
    if top["id"] != canonical_id or not top["content"].startswith("## H"):
        print(f"  - search top hit wrong: {top}")
        sys.exit(1)
    print(f"  + search returns canonical chunk with full content")

    # Cleanup
    d = await core.delete_memory({"id": canonical_id})
    if d.get("chunks_deleted") != chunks:
        print(f"  - delete didn't cascade: {d}")
        sys.exit(1)
    print(f"  + delete cascaded to all {chunks} chunks")

asyncio.run(main())
PY

log ""
log "==> 0.5.0-019 complete. Chunked embedding deployed."
log "    Content under 4000 chars: single point (zero overhead)."
log "    Content over 4000 chars: ~1500-char chunks with 200-char overlap,"
log "    linked via canonical_id. Search dedupes; one record per memory."
