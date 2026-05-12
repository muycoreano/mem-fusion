# Mem-Fusion — Tests & Dev Tooling

Manual, ephemeral, log-friendly. No launchd. No persistence beyond what you start. Stop a peer, it's gone until you start it again. Reboot the machine, no dev peer comes back automatically.

## Layout

```
tests/
├── README.md
├── lib/common.sh
├── setup-peer.sh, start-peer.sh, stop-peer.sh, restart-peer.sh,
│   teardown-peer.sh, status-peers.sh, logs-peer.sh, logs-all.sh
├── constellation/                  ← multi-peer federation tests (needs dev peers running)
│   ├── preload-memories.py         ← seeds peer-b and peer-c via core.store_memory
│   ├── test-promotion.py           ← peer promotes via /memory/put, reads back via /memory/get
│   └── test-directory.py           ← /peers and /peers/self aggregation
└── mem_fusion/                     ← single-node MCP tests (self-contained Qdrant on :6733)
    └── test-mcp-tools.py           ← exercises all 9 MCP tools via stdio JSON-RPC
```

Constellation tests assume the dev peers are running on `:6433`, `:6533`, `:6633`. The mem_fusion test spins up its own Qdrant in a tempdir and tears it down on exit — no peer setup required.

## Inner-loop workflow

```bash
./setup-peer.sh mem-fusion-dev      # one-time install
./start-peer.sh mem-fusion-dev      # start the peer
./logs-peer.sh mem-fusion-dev       # tail all its logs in another terminal
# ... edit code, drop extensions, etc. ...
./restart-peer.sh mem-fusion-dev    # reload after a code change
./stop-peer.sh mem-fusion-dev       # done for the session
```

## Multi-peer testing

```bash
./setup-peer.sh mem-fusion-peer-b   # one-time
./setup-peer.sh mem-fusion-peer-c   # one-time
./start-peer.sh mem-fusion-dev
./start-peer.sh mem-fusion-peer-b
./start-peer.sh mem-fusion-peer-c
./logs-all.sh                       # tail every running peer simultaneously
./status-peers.sh                   # see what's running
```

## Reset / cleanup

```bash
./teardown-peer.sh mem-fusion-peer-c   # wipe one peer entirely
```

## Known peers and their port assignments

See `lib/common.sh`:

| Peer name | Qdrant HTTP | Qdrant gRPC |
|---|---|---|
| `mem-fusion-dev` | 6433 | 6434 |
| `mem-fusion-peer-b` | 6533 | 6534 |
| `mem-fusion-peer-c` | 6633 | 6634 |

Add more peers by appending a line to the `PEER_PORTS` array in `lib/common.sh`.

## What dev peers share with production Mem-Fusion

- **Qdrant binary** — copied from production install (no separate download)
- **Python venv** — symlinked to production's venv (saves disk + setup time)
- **Ollama** — both production and dev consume the same Ollama on port 11434 (stateless service, no conflict)
- **`init_collection.py` script** — borrowed from production install

## What dev peers do NOT share with production

- **Qdrant data** — every peer has its own `qdrant-data/` and its own collection
- **MCP server process** — each peer runs its own `mcp_server.py` (patched to point at its own port + collection)
- **Hooks** — production hooks fire on YOUR live Claude sessions; dev peers have no hooks installed at all
- **launchd plists** — production has them (durability); dev peers don't (ephemeral)

## When NOT to use this tooling

- Production install (`~/.local/share/mem-fusion/`) — has its own install/upgrade flow via the Mem-Fusion repo's `INSTALL_MEM_FUSION.md`
- Anything you want to survive a reboot — dev peers don't auto-restart

## Convention for new scripts

If you add a new dev tooling script, source `lib/common.sh` and use the helpers (`peer_dir`, `peer_port`, `peer_is_running`, `require_peer_name`). Don't hardcode peer names or ports.
