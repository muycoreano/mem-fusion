#!/usr/bin/env bash
#
# build_install.sh — assemble the distribution INSTALL files from templates.
#
# Reads src/templates/INSTALL_MEM_FUSION.tmpl and INSTALL_CONSTELLATION.tmpl,
# expands the three INCLUDE directives, writes the built artifacts at repo root.
#
# Directive forms:
#   <!-- INCLUDE: <src> AS <dest> -->            verbatim heredoc, quoted (no shell expansion)
#   <!-- INCLUDE_EXEC: <src> AS <dest> -->       verbatim heredoc + chmod +x
#   <!-- INCLUDE_TEMPLATED: <src> AS <dest> -->  unquoted heredoc (${VAR} expands at install time)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TEMPLATES="$ROOT/src/templates"

emit_heredoc() {
    # $1 = directive (INCLUDE | INCLUDE_EXEC | INCLUDE_TEMPLATED)
    # $2 = src path relative to repo root
    # $3 = dest path on install target
    local directive="$1" src="$2" dest="$3"
    local src_path="$ROOT/$src"

    if [[ ! -f "$src_path" ]]; then
        echo "ERROR: included file does not exist: $src" >&2
        return 1
    fi

    # Per-file token derived from the destination path so heredocs never collide
    local token
    token="$(printf '%s' "$dest" | shasum -a 256 | cut -c1-12 | tr 'a-f' 'A-F')_EOF"

    case "$directive" in
        INCLUDE)
            printf '```bash\n'
            printf "cat > %s <<'%s'\n" "$dest" "$token"
            cat "$src_path"
            printf '%s\n```\n' "$token"
            ;;
        INCLUDE_EXEC)
            printf '```bash\n'
            printf "cat > %s <<'%s'\n" "$dest" "$token"
            cat "$src_path"
            printf '%s\n' "$token"
            printf 'chmod +x %s\n' "$dest"
            printf '```\n'
            ;;
        INCLUDE_TEMPLATED)
            printf '```bash\n'
            printf "cat > %s <<%s\n" "$dest" "$token"
            cat "$src_path"
            printf '%s\n```\n' "$token"
            ;;
        *)
            echo "ERROR: unknown directive: $directive" >&2
            return 1
            ;;
    esac
}

build_one() {
    local template="$1" output="$2"
    [[ -f "$template" ]] || { echo "ERROR: template not found: $template" >&2; return 1; }

    local tmp
    tmp="$(mktemp)"

    while IFS= read -r line; do
        if [[ "$line" =~ ^\<!--[[:space:]]+(INCLUDE|INCLUDE_EXEC|INCLUDE_TEMPLATED):[[:space:]]+(.+)[[:space:]]+AS[[:space:]]+(.+)[[:space:]]+--\>$ ]]; then
            local directive="${BASH_REMATCH[1]}"
            local src="${BASH_REMATCH[2]}"
            local dest="${BASH_REMATCH[3]}"
            emit_heredoc "$directive" "$src" "$dest" >> "$tmp"
        else
            printf '%s\n' "$line" >> "$tmp"
        fi
    done < "$template"

    mv "$tmp" "$output"
    echo "  ✓ built $(basename "$output") ($(wc -l < "$output" | tr -d ' ') lines)"
}

echo "→ building from $TEMPLATES"

build_one "$TEMPLATES/INSTALL_MEM_FUSION.tmpl"    "$ROOT/INSTALL_MEM_FUSION.md"
build_one "$TEMPLATES/INSTALL_CONSTELLATION.tmpl" "$ROOT/INSTALL_CONSTELLATION.md"

echo "→ done"
