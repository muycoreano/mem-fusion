# Constellation Extension

Bundled extension shipped with Mem-Fusion v0.3.0. Enables federated **group memory** across multiple Mem-Fusion nodes.

> **Architecture:** see [`docs/CONSTELLATION_ARCHITECTURE.md`](../../docs/CONSTELLATION_ARCHITECTURE.md)
> **Implementation plan:** see [`docs/V0.3.0-PROJECT-PLAN.md`](../../docs/V0.3.0-PROJECT-PLAN.md)

## How it relates to Mem-Fusion

| | Mem-Fusion | Constellation |
|---|---|---|
| Purpose | Personal memory on one machine | Group memory across machines |
| Transport | stdio MCP (per-Claude-session subprocess) | HTTP MCP (persistent daemon) |
| Network exposure | Localhost only | Network-bound for inter-peer reachability |
| Storage | `mem_fusion_memories` collection | `mem_fusion_canonical_memories` collection |
| Required? | Yes (base install) | No — optional extension |

The two daemons share Qdrant + Ollama infrastructure but operate on disjoint collections. Constellation has no privileged access to Mem-Fusion's local memory; cross-process boundaries enforce isolation.

## Running

```bash
# WP2 scope (scaffold only — no MCP tools yet)
~/.local/share/mem-fusion/venv/bin/python \
  ~/dev/mem-fusion/extensions/constellation/constellation.py \
  --config ~/.local/share/mem-fusion/constellation/config.json \
  --foreground
```

Process appears in `ps`/Activity Monitor as `python constellation.py …` — meaningful name rather than generic "daemon."

In production (WP4), the daemon runs under `launchd` via `com.branchapp.memfusion.constellation.plist`.

## Status

- ✅ WP1 — `export_record` tool added to Mem-Fusion (enables faithful memory propagation)
- 🚧 **WP2 — daemon scaffold (this commit)**
- ⏭️ WP3 — implement the 4 MCP tools (`memory/put`, `memory/get`, `peers`, `peers/self`)
- ⏭️ WP4 — launchd integration
- ⏭️ WP5 — Claude bridge (CLAUDE.md dual-call pattern)
- ⏭️ WP6 — multi-peer E2E verification
- ⏭️ WP7 — documentation + release
