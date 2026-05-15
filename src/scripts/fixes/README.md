# scripts/fixes/

Incremental, deployable bug-fix scripts. Claude runs these on instruction to remediate issues on an installed mem-fusion peer.

## Pattern

Each fix is a standalone idempotent shell script. Files are named:

    <version>-<sequence>-<short-description>.sh

For example:

    0.5.0-001-fix-qdrant-home-path.sh
    0.5.0-002-fix-some-other-thing.sh
    0.6.0-001-...

The `<version>` is the mem-fusion version that ships the fix. The `<sequence>` (`001`, `002`, …) provides deterministic ordering within a version. The `<short-description>` is for human readability.

## Invariants

- **Idempotent.** Safe to re-run. Detects "already fixed" state and exits 0 without changes.
- **Self-contained.** No external arguments. Reads its own environment.
- **Verbose-by-default.** Each step prints what it's about to do so Claude can render a per-step summary to the user.
- **Fail-loud.** `set -euo pipefail`. If a fix can't safely apply (e.g., a conflict that requires human review), surface the conflict and exit non-zero — never silently guess.
- **Single-responsibility.** One bug per script. Compose by running multiple scripts in sequence.

## When Claude runs a fix

1. **User reports a known bug** whose fix script is already in `fixes/`: Claude finds the matching script and runs it.
2. **Upgrade flow** (post-WP-2 install.sh): the upgrade phase scans `fixes/` and runs each script in sorted order, relying on idempotency to skip already-applied fixes.
3. **User explicitly asks** to apply a specific fix.

Claude always renders a per-step summary of the script's output to the user — never dumps raw script output.

## Adding a new fix

1. Pick the next sequence number for the current development version.
2. Create the script with the naming pattern above.
3. Document the bug in the script header comment (what it is, how it manifests, how the script remediates).
4. Make it idempotent.
5. Add a CHANGELOG entry under the appropriate version's `### Fixed` section.
6. Commit on the appropriate branch.
