# Mem-Fusion — Tests

All tests are self-contained. Each spins up its own Qdrant (and Constellation daemons, where needed) in a tempdir and tears everything down on exit. No external setup, no peer-management scripts, no shared state between runs.

## Layout

```
tests/
├── README.md
├── lib/
│   └── harness.py             ← shared Qdrant + Constellation lifecycle helpers
├── constellation/
│   ├── test-promotion.py      ← /memory/put integrity + additive-merge on hash collision (1 daemon, simulated peer push)
│   ├── test-directory.py      ← /peers + /peers/self aggregation (1 daemon, simulated peers via origin_node)
│   └── test-pull-push.py      ← end-to-end gateway /pull + /push + push-time group filter (2 daemons)
├── mem_fusion/
│   ├── test-mcp-tools.py             ← all 12 MCP tools (10 memory + group_pull + group_push) over stdio JSON-RPC (1 Qdrant + 1 mem-fusion)
│   ├── test-group-roundtrip.py       ← end-to-end: Claude → mem-fusion → Constellation → peer → search; also fires UserPromptSubmit hook against bob's install (2 Qdrants + 2 Constellations + 2 mem-fusions)
│   ├── test-store-now-share-later.py ← v0.4 add_groups + targeted memory_ids push + scope rule (3 Qdrants + 3 Constellations + 3 mem-fusions)
│   └── test-offline-rejoin.py        ← peer-symmetric catch-up: alice→bob (carol offline), bob→alice, bob goes offline, carol joins and pulls everything via alice
├── hooks/
│   └── test-hooks.py                 ← compile-check + end-to-end smoke for all 4 Claude Code hooks against a fake $HOME (1 Qdrant)
└── installer/
    └── test-installers.sh            ← staleness + bash-syntax check on INSTALL_*.md (no subprocesses; pure docs pipeline)
```

## What each test exercises

| Test | Scope |
|---|---|
| `test-mcp-tools.py` | Mem-fusion's stdio MCP server end-to-end — every tool incl. `add_groups`, plus graceful degradation when Constellation isn't installed. |
| `test-group-roundtrip.py` | Full chain: Claude → mem-fusion → Constellation gateway → peer's Constellation → peer's Qdrant → peer's mem-fusion finds it via `search_memory`. Covers store-then-share-via-add_groups, push and pull directions, offline-at-push recovery via pull. |
| `test-store-now-share-later.py` | The canonical v0.4 workflow with 3 peers (alice-origin + bob-eng + carol-prod). Verifies the scope rule (product push doesn't re-contact engineering peer) and the push-time group filter (receivers never see `personal` or unrelated group tags). |
| `test-offline-rejoin.py` | Peer-symmetric resilience with 3 engineering members where peers come online in sequence. Alice stores while bob+carol are offline → bob joins and pulls → bob stores + pushes (carol still offline) → bob goes offline → carol joins and pulls. Verifies carol gets the full 5-memory history from alice's relay despite bob being offline, with `origin_node` preserved end-to-end. |
| `test-promotion.py` | The receive side: integrity invariants on `/memory/put` (byte-identical content + vector, content_hash recompute, `groups` list, receiver-assigned id), duplicate detection on re-push, content_hash mismatch rejection, additive merge when receiver already has the content. |
| `test-directory.py` | The directory side: `/peers/self` shape, `/peers` aggregation (submission_count, first_seen/last_seen, sort order), negative cases (unknown group → 403, missing param → 400). |
| `test-pull-push.py` | Two daemons talking real HTTP: push fan-out, push idempotency, pull dedup, pull-privacy (personal-only memories stay local), pull catch-up after a drop, push-time group filter on the wire. |
| `test-installers.sh` | Build pipeline staleness check: rebuilds `INSTALL_MEM_FUSION.md` and `INSTALL_CONSTELLATION.md` from templates into a tempdir, diffs against the committed copies, then runs `bash -n` over every \`\`\`bash code block in the generated docs. Catches "forgot to rebuild after editing templates" and busted shell. |
| `test-hooks.py` | All four Claude Code hooks exercised end-to-end against a fake `$HOME` staged with the mem-fusion-shaped layout (real shell → Python → core chain). Compile-checks every hook (`bash -n` / `py_compile`), runs SessionStart snapshot, UserPromptSubmit injection (matching + short + unrelated prompts), PostToolUse:Write code capture, and Stop hook session ingest including idempotent dedup. Catches the `srv.tool_*`-class regression that ships symbol references but never exercises the actual import chain. |

## Run

```bash
~/.local/share/cowork-memory/venv/bin/python tests/mem_fusion/test-mcp-tools.py
~/.local/share/cowork-memory/venv/bin/python tests/mem_fusion/test-group-roundtrip.py
~/.local/share/cowork-memory/venv/bin/python tests/mem_fusion/test-store-now-share-later.py
~/.local/share/cowork-memory/venv/bin/python tests/mem_fusion/test-offline-rejoin.py
~/.local/share/cowork-memory/venv/bin/python tests/constellation/test-promotion.py
~/.local/share/cowork-memory/venv/bin/python tests/constellation/test-directory.py
~/.local/share/cowork-memory/venv/bin/python tests/constellation/test-pull-push.py
~/.local/share/cowork-memory/venv/bin/python tests/hooks/test-hooks.py
tests/installer/test-installers.sh
```

Each prints a per-step trace and a final `N/N invariants passed` line followed by a pass/fail summary.

## Port allocation (tempdir-scoped, won't collide with production or each other)

| Test | Qdrant | Peer | Gateway |
|---|---|---|---|
| `test-mcp-tools.py` | 6733 | — | — |
| `test-promotion.py` | 6733 | 7733 | 7734 |
| `test-directory.py` | 6743 | 7743 | 7744 |
| `test-pull-push.py` (peer-a) | 6833 | 7833 | 7834 |
| `test-pull-push.py` (peer-b) | 6933 | 7933 | 7934 |
| `test-group-roundtrip.py` (alice) | 6843 | 7843 | 7844 |
| `test-group-roundtrip.py` (bob) | 6943 | 7943 | 7944 |
| `test-store-now-share-later.py` (alice) | 6473 | 7473 | 7474 |
| `test-store-now-share-later.py` (bob)   | 6483 | 7483 | 7484 |
| `test-store-now-share-later.py` (carol) | 6493 | 7493 | 7494 |
| `test-offline-rejoin.py` (alice)        | 6543 | 7543 | 7544 |
| `test-offline-rejoin.py` (bob)          | 6553 | 7553 | 7554 |
| `test-offline-rejoin.py` (carol)        | 6563 | 7563 | 7564 |
| `test-hooks.py`                          | 6753 | — | — |

If a test crashes mid-run, the subprocess may linger and hold its port. `lsof -nP -iTCP:<port> -t | xargs kill` clears it.

## Adding a test

Use `tests/lib/harness.py`:

```python
import pathlib, sys, tempfile
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "lib"))
import core, harness

with tempfile.TemporaryDirectory() as tdir:
    tmp = pathlib.Path(tdir)
    qdrant = harness.start_qdrant(6733, tmp, tmp / "qdrant.log")
    harness.init_cowork_memories(6733)
    # ...
```

The harness exposes: `start_qdrant`, `init_cowork_memories`, `write_constellation_config`, `start_constellation`, `wait_health`, `check`, `stop_proc`.
