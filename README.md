# Mem-Fusion

```
███╗   ███╗███████╗███╗   ███╗      ███████╗██╗   ██╗███████╗██╗ ██████╗ ███╗   ██╗
████╗ ████║██╔════╝████╗ ████║      ██╔════╝██║   ██║██╔════╝██║██╔═══██╗████╗  ██║
██╔████╔██║█████╗  ██╔████╔██║█████╗█████╗  ██║   ██║███████╗██║██║   ██║██╔██╗ ██║
██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║╚════╝██╔══╝  ██║   ██║╚════██║██║██║   ██║██║╚██╗██║
██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║      ██║     ╚██████╔╝███████║██║╚██████╔╝██║ ╚████║
╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝      ╚═╝      ╚═════╝ ╚══════╝╚═╝ ╚═════╝ ╚═╝  ╚═══╝
```

| ⚠️  IMPORTANT! PROJECT NO LONGER UNDER ACTIVE SPONSORSHIP AND SUPPORT |
| :--- |
| Mem-fusion was an experimental project that Branch sponsored to test AI capability development. While successful at increasing local memory capabilities of Claude, long-term development and maintenance has been deemed not viable given the pace and competition in the memory capability space by incumbents and startups. Specifically, the problems mem-fusion was built to address are already being actively developed by Anthropic. |
| I'll continue using it for local memory and personal p2p machine synchronization, and assisting anyone else wanting to use mem-fusion — but the project is no longer under active sponsorship or support from Branch. |

**Persistent memory for Claude Code, running locally on your machine.**

Claude Code starts every session blank. Mem-Fusion captures decisions, preferences, errors, and code patterns as you work, and silently surfaces them when they're relevant later. The longer you use it, the sharper your Claude gets at *your* work in *your* codebase.

Everything runs locally. Nothing leaves your machine unless you explicitly share it.

---

## Install

One curl line. macOS + Homebrew + Claude Code required.

```bash
curl -fsSL https://raw.githubusercontent.com/muycoreano/mem-fusion/main/install.sh | bash
```

Re-run any time to upgrade — the installer is idempotent. ~200 MB on disk; brings up Qdrant, Ollama + `nomic-embed-text`, the MCP server, 4 hooks, and 2 skills.

---

## Using it

Most of the time, do nothing. Four Claude Code hooks watch your sessions, inject relevant past memories before Claude responds, and capture decisions / preferences / errors / code as you work.

When you want to pin something explicitly:

```
You:    /remember always use uv for Python environments

Claude: ✓ Stored as preference (id: a7e3c2d1). Local-only.
```

After a long working session, ask Claude to consolidate:

> *"Let's pause and reflect on what we learned. Update your memory and make sure to update mem-fusion as well."*

---

## Sharing memories across machines or with a team

Two paths, both optional, neither required for local-only use:

- **Connectors** — bridge mem-fusion to productivity tools you already use. Slack ships as the v0.5 example; others are designed but not yet implemented. See [`docs/v0.5_CONNECTOR_ARCHITECTURE.md`](docs/v0.5_CONNECTOR_ARCHITECTURE.md).
- **Constellation** — direct peer-to-peer LAN sync for environments where cloud tools aren't appropriate. See [`constellation/README.md`](constellation/README.md).

Both implement the same `push` / `pull` / `status` / `since` API.

---

## Status & limitations (alpha)

This is a working alpha. The local-memory layer is reliable; everything else has rough edges. Before you commit your workflow to it, know:

- **macOS-only** installer. No Linux or Windows support yet.
- **Local daemons required.** Qdrant + Ollama run in the background as launchd agents (~200 MB resident, plus the embedding model). If they die, memory writes/reads fail until they come back.
- **Cross-machine personal sync is not yet shipped.** v0.6 design exists at [`docs/v0.6_PERSONAL_SYNC.md`](docs/v0.6_PERSONAL_SYNC.md); the implementation past the foundation prototype is paused while we re-evaluate the approach against simpler alternatives (e.g., a hosted Qdrant both machines point at).
- **Slack is the only working connector** today. GDrive / Teams / Discord / Notion are designed but not implemented.
- **Constellation is experimental.** P2P over LAN works; it has not been hardened against adversarial peers, NAT traversal, or large meshes.
- **Identity model has sharp edges.** `origin_node` resolves from your Constellation config or the OS hostname; two peers with the same `node_name` will misroute the loopback gate. Set a unique `node_name` in `constellation/config.json`.
- **Schemas may change.** Wire formats (`docs/v0.5_CONNECTOR_ARCHITECTURE.md`), content_hash spec, and connector config can break across versions. Fix scripts (`src/scripts/fixes/`) handle migrations on installed peers, but expect occasional reset work.
- **Claude Desktop / Cowork is not supported.** The desktop app runs in a sandbox that can't reach the local filesystem the same way the CLI can. Mem-Fusion is CLI-only.
- **Single-user assumption.** No multi-tenant model. The same user account is assumed on all peers in a `personal` group.
- **Embedding model is fixed.** nomic-embed-text (768-dim) via Ollama. Swapping is possible but not currently a knob.
- **Not production-tested at scale.** Verified working at ~450 memories on a single peer; not benchmarked beyond.

If something doesn't work the way the docs say it should, see [`HOW-TO-CONTRIBUTE.md`](HOW-TO-CONTRIBUTE.md) for how to report it.

---

## Further reading

- [`HOW-TO-CONTRIBUTE.md`](HOW-TO-CONTRIBUTE.md) — bug reports, enhancement proposals, code changes
- [`docs/MEM_FUSION_ARCHITECTURE.md`](docs/MEM_FUSION_ARCHITECTURE.md) — storage model, MCP tools, hooks, lifecycle
- [`docs/v0.5_CONNECTOR_ARCHITECTURE.md`](docs/v0.5_CONNECTOR_ARCHITECTURE.md) — connector wire format and dispatch
- [`constellation/README.md`](constellation/README.md) — P2P sibling system
- [`CHANGELOG.md`](CHANGELOG.md) — what's in each version

---

## Uninstall

```bash
claude mcp remove mem-fusion
launchctl bootout gui/$UID/com.branchapp.memfusion.{qdrant,ollama,constellation}
rm ~/Library/LaunchAgents/com.branchapp.memfusion.*.plist
rm -rf ~/.local/share/mem-fusion        # deletes all stored memories
rm -rf ~/.claude/skills/{remember,mem-fusion-slack-connector}
```

Then strip the `<!-- mem-fusion:* -->` blocks from `~/CLAUDE.md` and `~/.claude/settings.json`.

---

## License

MIT. See [`LICENSE`](LICENSE).

*Built at [Branch](https://branchapp.com).*
