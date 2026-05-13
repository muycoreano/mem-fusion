# Mem-Fusion — Tests

All tests are self-contained. Each spins up its own Qdrant (and Constellation daemons, where needed) in a tempdir and tears everything down on exit. No external setup, no peer-management scripts, no shared state between runs.

## Layout

```
tests/
├── README.md
├── lib/
│   └── harness.py             ← shared Qdrant + Constellation lifecycle helpers
├── constellation/
│   ├── test-promotion.py      ← /memory/put + /memory/get + content_hash integrity (1 daemon, simulated peer push)
│   ├── test-directory.py      ← /peers + /peers/self aggregation (1 daemon, simulated peers via origin_node)
│   └── test-pull-push.py      ← end-to-end gateway /pull + /push (2 daemons, real peer fan-out)
└── mem_fusion/
    └── test-mcp-tools.py      ← all 11 MCP tools (9 memory + group_pull + group_push) over stdio JSON-RPC
```

## What each test exercises

| Test | Scope |
|---|---|
| `test-mcp-tools.py` | Mem-fusion's stdio MCP server end-to-end — every tool, plus graceful degradation when Constellation isn't installed. |
| `test-promotion.py` | The receive side: integrity invariants on `/memory/put` (byte-identical content + vector, content_hash recompute, group_name tagging, receiver-assigned id), duplicate detection on re-push, content_hash mismatch rejection. |
| `test-directory.py` | The directory side: `/peers/self` shape, `/peers` aggregation (submission_count, first_seen/last_seen, sort order), negative cases (unknown group → 403, missing param → 400). |
| `test-pull-push.py` | Two daemons talking real HTTP: push fan-out, push idempotency, pull dedup, pull-privacy (non-pushed memories stay local), pull catch-up after a drop. |

## Run

```bash
~/.local/share/cowork-memory/venv/bin/python tests/mem_fusion/test-mcp-tools.py
~/.local/share/cowork-memory/venv/bin/python tests/constellation/test-promotion.py
~/.local/share/cowork-memory/venv/bin/python tests/constellation/test-directory.py
~/.local/share/cowork-memory/venv/bin/python tests/constellation/test-pull-push.py
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
