#!/usr/bin/env bash
# Fix 0.5.0-015 — canonical content_hash (NFC + UTF-8 + sha256 full 64-char)
# + Stage 1 substrate deploy (connectors/ package, patched core.py)
#
# Implements docs/v0.5_CONTENT_HASH_SPEC.md (architect-acked per audit
# sign-off memory 250e8a54) AND deploys the Stage 1.1+1.2 substrate
# refactor that was committed but not yet rolled out to running peers
# (commits d647b80 + ea7c6bf).
#
# DEPLOYS:
#
#   A. src/connectors/ package → ~/.local/share/mem-fusion/connectors/
#      (Stage 1.1 — was never deployed via a fix script; this script
#      catches that gap):
#      - connectors/__init__.py        (registry + factory)
#      - connectors/base.py            (Connector ABC: format_envelope,
#                                       parse_envelope, verify_integrity,
#                                       build_envelope_from_record)
#      - connectors/envelope.py        (Envelope TypedDict + validation)
#      - connectors/slack.py           (SlackConnector with §5.1 wire format
#                                       and NFC-canonical _wire_hash)
#
#   B. Patched core.py:
#      - `unicodedata` added to imports.
#      - `content_hash(text)` rewritten to NFC → UTF-8 → SHA-256 → full
#        64-char lowercase hex. Drops `.strip()`, `.lower()`, and `[:16]`.
#        Behavior changes (architect-acked per spec §4.1): case-folding
#        removed (Hello ≠ hello), whitespace-stripping removed
#        (" hello" ≠ "hello"), truncation removed (collision margin), NFC
#        added (combining-acute / precomposed accents unify across peers).
#      - `store_memory_from_envelope` substrate-agnostic — delegates the
#        integrity check to `get_connector(connector_type).verify_integrity`.
#
#   C. Qdrant payload migration sweep:
#      - For every memory in the local collection, recompute
#        `content_hash(payload.content)` using the new algorithm and
#        update the payload's `content_hash` field via `set_payload`.
#      - Idempotent: re-running on already-canonical hashes is a no-op
#        (compared-and-skipped per point).
#      - The Qdrant `content_hash` payload index already supports
#        variable-length keyword strings; no index migration needed.
#
# NOT IN SCOPE FOR THIS FIX:
#   - Coordinated cross-peer migration. Each peer runs this script on its
#     own machine; receiver-side dedup absorbs any timing skew naturally
#     (peer A migrates first, receives unmigrated B's envelope, parses
#     fine, integrity-checks fine — the wire hash is also canonical, so
#     identical content has identical wire hash regardless of which peer
#     ran the script first).
#   - Bumping `envelope_version` — not required: the wire format's bytes
#     don't change shape, only the digest function. Receivers see the
#     same envelope schema; the content_hash field is still a sha256 hex.
#
# Idempotent. Verified by tests/lib/test-content-hash-canonical.py
# (18 checks: spec §4.3 reference vectors, NFC equivalence, NFKC distinction,
# case-folding removed, whitespace-stripping removed, wire/local agreement,
# cross-peer Unicode drift round-trip) and
# tests/mem_fusion/test-connector-e2e-roundtrip.py (16 checks including
# the back-compat regression on pre-refactor message bodies).
#
# Run via: bash 0.5.0-015-content-hash-canonical.sh

set -euo pipefail

MEMFUSION="${HOME}/.local/share/mem-fusion"
CORE_PY="${MEMFUSION}/core.py"
CONNECTORS_DIR="${MEMFUSION}/connectors"
VENV_PY="${MEMFUSION}/venv/bin/python"
QDRANT_BASE="http://127.0.0.1:6333"
COLLECTION="cowork_memories"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SRC_CORE="${SRC_ROOT}/core.py"
SRC_CONNECTORS="${SRC_ROOT}/connectors"

log() { printf "  %s\n" "$*"; }

echo "==> Fix 0.5.0-015: Stage 1 substrate deploy + canonical content_hash"

# Pre-flight
[[ -f "$CORE_PY" ]]        || { log "ERROR: $CORE_PY not found"; exit 1; }
[[ -x "$VENV_PY" ]]        || { log "ERROR: $VENV_PY not found"; exit 1; }
[[ -f "$SRC_CORE" ]]       || { log "ERROR: $SRC_CORE not found — run from a mem-fusion repo checkout"; exit 1; }
[[ -d "$SRC_CONNECTORS" ]] || { log "ERROR: $SRC_CONNECTORS not found"; exit 1; }
[[ -f "$SRC_CONNECTORS/__init__.py" ]] || { log "ERROR: $SRC_CONNECTORS/__init__.py missing"; exit 1; }
[[ -f "$SRC_CONNECTORS/base.py" ]]     || { log "ERROR: $SRC_CONNECTORS/base.py missing"; exit 1; }
[[ -f "$SRC_CONNECTORS/envelope.py" ]] || { log "ERROR: $SRC_CONNECTORS/envelope.py missing"; exit 1; }
[[ -f "$SRC_CONNECTORS/slack.py" ]]    || { log "ERROR: $SRC_CONNECTORS/slack.py missing"; exit 1; }
curl -s "${QDRANT_BASE}/healthz" >/dev/null || { log "ERROR: Qdrant unreachable at ${QDRANT_BASE}"; exit 1; }

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix A — deploy connectors/ package (Stage 1.1 + Stage 1 substrate)
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── A: deploy connectors/ package ──"
mkdir -p "$CONNECTORS_DIR"
NEED_DEPLOY=0
for fname in __init__.py base.py envelope.py slack.py; do
  if [[ ! -f "$CONNECTORS_DIR/$fname" ]] || ! cmp -s "$SRC_CONNECTORS/$fname" "$CONNECTORS_DIR/$fname"; then
    NEED_DEPLOY=1
    break
  fi
done
if [[ $NEED_DEPLOY -eq 0 ]]; then
  log "  Already deployed: connectors/ package matches canonical."
else
  for fname in __init__.py base.py envelope.py slack.py; do
    if [[ -f "$CONNECTORS_DIR/$fname" ]] && ! cmp -s "$SRC_CONNECTORS/$fname" "$CONNECTORS_DIR/$fname"; then
      BACKUP="${CONNECTORS_DIR}/${fname}.bak.0.5.0-015"
      log "  Backing up $fname to $(basename "$BACKUP")"
      cp "$CONNECTORS_DIR/$fname" "$BACKUP"
    fi
    cp "$SRC_CONNECTORS/$fname" "$CONNECTORS_DIR/$fname"
    log "  Deployed connectors/$fname"
  done
  "$VENV_PY" -c "
import sys
sys.path.insert(0, '$MEMFUSION')
from connectors import get_connector, list_connector_types
sc = get_connector('slack')
assert sc.type == 'slack'
assert hasattr(sc, 'verify_integrity')
assert hasattr(sc, 'build_envelope_from_record')
print('  connectors/ package imports + ABC methods present')
"
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix B — deploy patched core.py (canonical content_hash + connector dispatch)
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── B: deploy patched core.py (canonical content_hash + Stage 1 receive flow) ──"
if grep -q 'unicodedata.normalize("NFC", text)' "$CORE_PY" \
   && grep -q 'connector.verify_integrity(envelope)' "$CORE_PY"; then
  log "  Already applied: NFC content_hash + connector delegation present."
else
  BACKUP="${CORE_PY}.bak.0.5.0-015"
  log "  Backing up to $BACKUP"
  cp "$CORE_PY" "$BACKUP"
  log "  Deploying canonical $SRC_CORE → $CORE_PY"
  cp "$SRC_CORE" "$CORE_PY"
  "$VENV_PY" -c "import ast; ast.parse(open('$CORE_PY').read())" \
    && log "  syntax ok" \
    || { log "  ERROR: deployed core.py failed to parse — restoring backup"; cp "$BACKUP" "$CORE_PY"; exit 1; }
fi

# ──────────────────────────────────────────────────────────────────────────
# Sub-fix C — Qdrant payload migration sweep
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── C: Qdrant payload migration sweep (idempotent) ──"
"$VENV_PY" <<'PY'
import os, sys
sys.path.insert(0, os.path.expanduser("~/.local/share/mem-fusion"))
import core
from qdrant_client.http.models import Filter

total = 0
migrated = 0
already = 0
empty_content = 0
offset = None
while True:
    points, next_off = core.qdrant.scroll(
        collection_name=core.COLLECTION,
        scroll_filter=Filter(must=[]),
        offset=offset,
        limit=256,
        with_payload=True,
        with_vectors=False,
    )
    if not points:
        break
    for p in points:
        total += 1
        pl = p.payload or {}
        content = pl.get("content")
        if not isinstance(content, str) or not content:
            empty_content += 1
            continue
        new_hash = core.content_hash(content)
        old_hash = pl.get("content_hash", "")
        if old_hash == new_hash:
            already += 1
            continue
        core.qdrant.set_payload(
            collection_name=core.COLLECTION,
            payload={"content_hash": new_hash},
            points=[p.id],
        )
        migrated += 1
    if next_off is None:
        break
    offset = next_off

print(f"  total points scanned   : {total}")
print(f"  migrated to canonical  : {migrated}")
print(f"  already canonical      : {already}")
print(f"  empty content (skipped): {empty_content}")
PY

# ──────────────────────────────────────────────────────────────────────────
# Verify
# ──────────────────────────────────────────────────────────────────────────
log ""
log "── verify: spec §4.3 reference vectors agree on deployed core.py ──"
"$VENV_PY" <<'PY'
import os, sys
sys.path.insert(0, os.path.expanduser("~/.local/share/mem-fusion"))
import core
vectors = [
    ("",       "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ("hello",  "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"),
    ("hello\n","5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"),
]
ok = True
for text, expected in vectors:
    actual = core.content_hash(text)
    mark = "✓" if actual == expected else "✗"
    print(f"  {mark} content_hash({text!r}) = {actual[:16]}…")
    ok = ok and actual == expected
sys.exit(0 if ok else 1)
PY

log ""
log "==> 0.5.0-015 complete. Stage 1 substrate deployed + canonical content_hash + payload migrated."
log "    Re-run is safe (idempotent: already-canonical hashes skipped)."
