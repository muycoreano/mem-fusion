# mem-fusion — Project Update v1

**Date:** 2026-05-15
**Author:** Claude (PM/efficiency review from tk421-air2025)
**Period covered:** 2026-05-08 (v0.1.0 ship) → 2026-05-15 (this snapshot)
**Sources:** mem-fusion vector store (~85 records), #branch-ai-mem-fusion Slack channel (C0B3MLAFNM8), local CTO Assistant drafts
**Cadence:** ad-hoc; future updates as `Project_Update_MemFusion_v<N>_<date>_<time>.md`

---

## Executive Summary

mem-fusion is **on track and accelerating**. In the past 7 days, the project has gone from v0.1.0 ship → v0.3.1 → v0.4 design + implementation → v0.5 architecture lock + first WPs shipped, with **multi-machine personal memory sync now working end-to-end** (70+ memories synced mc-macbookpro → mc-macbookair, plus this peer joined today). The level-1 outcome Mitch declared gating — "if personal device sync doesn't work, the rest is worthless" — is **proven**.

Three signals stand out:

1. **External users are landing real bugs.** Atif found the install-hook attribute error and the `--scope user` MCP gap within hours of joining. William hit the cold-start uselessness loop on a brand-new install. Both fixed same-day. Test count grew **171 → 202 invariants** in one push.
2. **Velocity is sustainable but design-heavy.** Four design docs in 48 hours (v0.4 memory architecture, v0.5 architecture, v0.5 Slack connector, v0.6 artifact handling). Mitch has correctly scoped v0.6 separately and put mc-macbookpro into "design mode, not implementation mode" — this prevents the multi-machine implementation churn that briefly happened on 2026-05-15 (the now-removed `Plan_Slack_Driver_Milestones_2026-05-15_0420.md`).
3. **External forcing function is now architectural, not internal.** CrowdStrike Falcon + ThreatLocker EDR on mc-macbookpro blocks inbound TCP at the kernel level. This isn't a bug — it's the corporate trust regime mem-fusion has to coexist with. The v0.5 Slack connector is the durable answer.

**Health: green.** **Velocity: high.**

---

## Goals

Mem-fusion is layered. Each level is a precondition for the next.

| Level | Outcome | Gating? | Status |
|---|---|---|---|
| **1** | Single-user, single-machine persistent semantic memory across Claude Code sessions | Yes (v0.1.0 ship) | ✅ Shipped 2026-05-08 |
| **2** | Single-user, **multi-machine** personal memory sync | Yes ("if this doesn't work, the rest is worthless") | ✅ Proven 2026-05-14 (70 memories mc-macbookpro → mc-macbookair) |
| **3** | Multi-user **team groups** with explicit, opt-in sharing | No (next gate) | 🟡 Architected (v0.4 group routing + v0.5 connectors); not exercised end-to-end |
| **4** | Cross-network team sync (not just LAN) via Slack-as-transport | No (unblocks team adoption on corporate Macs) | 🟡 Designed (v0.5 Slack connector); pre-implementation |
| **5** | Public OSS with adoption flywheel | No (positioning) | 🟢 Active — `github.com/muycoreano/mem-fusion`, MIT, 5 external collaborators in channel |
| **6** | claude-mod rebrand (Monet / Debussy / Shannon …) at v1.0 | No (deferred) | ⏸ Brand locked; no work scheduled |

The project mission is **"fusion of memories"** — across cognitive layers (vertical), across machines (horizontal), across sessions (temporal). Every active initiative below maps to one of those axes.

---

## Current Status

### Shipped / live in production

| Version | Ship date | What |
|---|---|---|
| v0.1.0 | 2026-05-08 | Public release. Personal memory only; bundled `/remember` skill; 4 hooks; 8 MCP tools |
| v0.3.x line | 2026-05-13 | Tagged `v0.3.1`; maintenance branch `v0.3.x`. v0.3 work consolidated |
| v0.4 (effective) | 2026-05-13 → 2026-05-14 | Group-based routing replaces content classification; `group_pull` / `group_push` MCP tools; Constellation daemon (peer surface 7533, gateway 7534) with two-listener architecture; 202 test invariants pass; "store-now-share-later" workflow (`add_groups` retroactive widening); `--scope user` MCP fix; install-hook attribute fix; cold-start preload + `~/CLAUDE.md` auto-merge |
| v0.5.0-alpha | 2026-05-15 | Version bumped on `main` (`src/constellation.py`); `[Unreleased]` block opened in CHANGELOG; fix scripts pattern established |

### v0.5 work packages — current state

The v0.5 plan has **13 active work packages** plus one removed (WP-A: tag-v0.4-final, dropped because pre-alpha doesn't warrant the ceremony).

| WP | Title | Status | Notes |
|---|---|---|---|
| **WP-1** | `/remember` autopush + privacy invariant | ✅ Implemented 2026-05-15, committed | personal-with-peers auto-pushes; bare `push` = personal only; non-personal groups require explicit naming |
| **WP-4** | Qdrant `$HOME` literal path bug | ✅ Implemented as fix-script `0.5.0-001-fix-qdrant-home-path.sh` | YAML doesn't expand env vars — fix migrates `~/.local/share/mem-fusion/$HOME/...` → intended path |
| **WP-J** *(implicit)* | cowork-memory → mem-fusion rename | ✅ Implemented as fix-script `0.5.0-002` | Relabels launchd agents, moves Qdrant infrastructure, patches `.claude.json` |
| **WP-E** | SlackConnector implementation | 🟡 Design only — `docs/v0.5_SLACK_CONNECTOR.md` landed on `v0.5` branch (commit `a40e858`) 2026-05-15 | Two design drafts exist: the GitHub-tracked architect doc and `Plan_Constellation_v0.5_SlackConnector_Design_2026-05-15_0930.md` (this peer); need reconciliation |
| **WP-F** | Slack wire format | 🟡 Design covered in WP-E doc | JSON envelope + human-readable body |
| **WP-2** | `install.sh` as primary install path | ⏸ Not yet started | Replaces paste-INSTALL-into-Claude pattern |
| **WP-3** | Post-install Claude knowledge handoff | ⏸ Not yet started | Three-layer: in-session payload + mem-fusion store + CLAUDE.md write |
| **WP-B** | Extract Connector interface; refactor v0.4 HTTP code into HttpConnector | ⏸ Not yet started | Zero-behavior-change refactor — prereq for SlackConnector |
| **WP-C** | Daemon scheme→executor registry | ⏸ Not yet started | Including daemon-delegates-to-Claude path |
| **WP-D** | URL-shaped peer-config schema | ⏸ Not yet started | Migrate v0.4 `{node_name, endpoint}` → `slack:#chan` / `http://...` strings |
| **WP-G** | Connector SDK doc | ⏸ Not yet started | Interface contract, timestamp semantics, idempotency |
| **WP-H** | End-to-end validation | ⏸ Pending WP-B…WP-F | mc-macbookpro on `slack:` + mc-macbookair on `http:` in same group |
| **WP-I** | Propagating edit primitive | ⏸ Open design question | `canonical_id` retrofit vs first-class `POST /memory/edit` vs Slack threads as edit graph (proposed in this peer's draft) |

**Operational hardening shipped in parallel:**
- `launchctl load` + `launchctl kickstart -p` pattern established as a convention for fix scripts (after twice observing pended-spawn on mc-macbookair)
- Post-commit cycle codified as a durable workflow rule (`feedback_post_commit_cycle.md` + memory `8bcc1fb0`)
- Cross-machine role assignment locked: mc-macbookpro = architect; mc-macbookair = storage/transport implementation; tk421-air2025 = personal-sync peer only

### v0.6 — scoped separately

- **Artifact handling design draft** at `~/CTO Assistant/drafts/v0.6_ARTIFACT_HANDLING.md` (369 lines, 20 KB on mc-macbookpro — not present on this peer).
- Introduces `kind: memory | artifact` discriminator flowing through MCP tools, wire format, connector interface.
- WP-α (per-peer per-kind cursor) is a v0.4 cursor-bug fix that also unblocks v0.6 artifact work.
- **Awaiting Mitch's redline of 7 open questions** before implementation begins. Correctly deferred from v0.5.

---

## Open Initiatives

Ranked by impact-to-goal-progression.

### 1. Land WP-B / WP-C / WP-D / WP-E (connector layer + SlackConnector) — **critical path**

This is the unlock for Level 3 (team groups) and Level 4 (cross-network). Until the SlackConnector ships, mc-macbookpro can't participate as a peer because EDR blocks inbound TCP. Every additional design iteration without code is delayed validation.

**Risk:** Two SlackConnector design docs exist (GitHub `v0.5` branch vs local tk421 draft). Reconciliation needed before implementation begins on mc-macbookair.

### 2. Land WP-2 + WP-3 (`install.sh` + post-install knowledge handoff) — **adoption critical**

Paste-INSTALL-into-Claude is paying daily token cost. Atif and William both hit install-time issues that would have been impossible with a deterministic script. The cold-start fix shipped 2026-05-14 buys time but doesn't eliminate the root cause.

### 3. Resolve WP-I (propagating edit primitive) — **correctness critical**

v0.4's content_hash dedup means edits look new on peers → stale-copy accumulation on every upsert+push. We've already seen 3 revisions of the v0.5 architecture decision land as separate memories rather than one with edit history. Design choice (canonical_id, first-class edit endpoint, or Slack threads as edit graph) is open.

### 4. Exercise level 3 (team group) end-to-end — **goal validation**

Atif, Shane, William are installed. None have a team group configured yet. The next external-user milestone is one of them pushing a memory to a shared group and another pulling it. Until that happens, level 3 is architected but unproven.

### 5. v0.6 redline + WP-α (per-peer per-kind cursor) — **lurking correctness bug**

The cursor bug (tk421 thought it was up-to-date, was missing 75 memories) is real and not fixed yet. WP-α is the architectural fix and is a hard prereq for artifacts. Should not start implementation until v0.5 connector layer is in tree.

---

## Velocity

### Slack activity in #branch-ai-mem-fusion (last 48h)

- **7 shipped-commit announcements** from Mitch (each accompanies a `git push` to `muycoreano/mem-fusion`)
- **4 design docs published** (v0.4 memory architecture, v0.5 Slack connector, v0.4 store-now-share-later refinement, v0.5 design)
- **5 external collaborators** in channel (Atif, Shane, Jake, Matt, William); Atif and Shane installed; William ran live install with issues that fed fixes back
- **2 external bug catches** turned into shipped tests within hours (Atif: install-hook attribute names; William: cold-start uselessness loop)

### GitHub commits visible from Slack announcements

| Commit | Date | Scope |
|---|---|---|
| `067f6c0` | 2026-05-14 14:18 | Cold-start fix: `~/CLAUDE.md` auto-merge + 8 preloaded usage memories + import local memories + INCLUDE_INLINE build directive |
| `1be857b` | 2026-05-14 11:38 | `--scope user` MCP registration fix |
| `a86b32f` | 2026-05-14 11:22 | Smoke-test end-to-end coverage; QDRANT_URL/OLLAMA_URL env-configurable |
| (untracked id) | 2026-05-14 01:32 | Hook attribute fix from Atif's report; tests 171 → 202 |
| `b39691a..c6839a2` | 2026-05-15 03:12 | v0.5 opening: `/remember push` privacy invariant + fix-scripts pattern + version bump to 0.5.0-alpha |
| `a40e858` | 2026-05-15 11:52 | `docs/v0.5_SLACK_CONNECTOR.md` design doc on `v0.5` branch |

### Memory-system meta-velocity

- Vector store on this peer: 11 → **~85** memories arrived via pull during this session (cross-machine drift caught up via mc-macbookair `personal` peer)
- 6 v0.5-specific decisions / design entries landed in last 48h (3 revisions of the v0.5 architecture decision alone)
- This peer pushed 2 new memories back upstream (Constellation install milestone + SlackConnector draft pointer)

**Assessment:** Velocity is high and *the dogfooding loop is working* — the memory system is being used to coordinate the memory system's own development across machines. Each Claude session inherits the full project state without manual handoff.

---

## Forecast — next 1–2 weeks

### What's likely to ship

| Item | Confidence | When | Why |
|---|---|---|---|
| **WP-B**: HttpConnector extraction (zero behavior change) | High | Within 3 days | Pure refactor; mc-macbookair has clear remit; no design churn possible |
| **WP-D**: URL-shaped peer config + v0.4 migration | High | Within 5 days | Mechanical; the architectural decision is locked |
| **WP-C**: Scheme→executor registry (HTTP only, Slack stubbed) | Medium | Within 7 days | The Claude-delegation path adds complexity worth landing as a stub first |
| **WP-E/F**: SlackConnector minimal viable (push-only, threads optional, no archives) | Medium | Within 10 days | Years-scale features (archive anchors, threads-as-edit-graph) defer to v0.5.1 |
| **WP-2**: `install.sh` primary install path | Medium | Within 10 days | Implementation is straightforward; the design choice (handoff mechanism) is the time sink |
| **WP-α**: Per-peer per-kind cursor (v0.4 cursor bug fix) | Medium | Within 14 days | Could ship as v0.4.1 ahead of v0.6 artifact work |

### What's at risk of slipping

- **WP-I** (propagating edits). Design is genuinely open; no obvious winner among the three approaches. Likely punted to v0.5.1 or v0.6.
- **WP-G** (Connector SDK doc). Doc tasks always slip when implementation accelerates. Mitigation: write SDK doc as part of WP-E so it stays current.
- **v0.6 artifact handling** redline. 7 open questions need Mitch's pass; until then v0.6 implementation can't begin. Reasonable for the team to focus on v0.5 first.
- **v0.5.0 release tag**. No date set. Per current convention ("pre-alpha doesn't warrant ceremony"), tagging happens when WP-E/F land at a usable bar — likely 2 weeks out.

### Expected memory-system metrics in 2 weeks

- ~150-200 total memories on personal-group peers (current trajectory)
- 2-3 team-group peers configured (Atif's eng team, possibly Shane)
- First non-Mitch shipped commit (Atif has already patched bugs in-place; first PR-style contribution likely within 2 weeks)
- Public mem-fusion installs: currently 2 (Mitch, Shane) + 1 attempted (William); realistic target 5-7

---

## Alignment to Goals

| Goal | Current alignment | Drift risk |
|---|---|---|
| **Level 1** — single-machine semantic memory | ✅ Fully aligned. Production usage by Mitch + Atif + Shane. Cold-start fix closed the only known regression. | None |
| **Level 2** — multi-machine personal sync | ✅ Aligned. Proven 2026-05-14 (70 memories synced). Cross-peer cursor bug is the only correctness gap, with a known workaround. | Low |
| **Level 3** — team groups | 🟡 Architected, not exercised. WP-1 (privacy invariant) locks the safety story; no team group has been used end-to-end yet. | Medium — would benefit from one team-group experiment with Atif or Shane to validate before SlackConnector lands |
| **Level 4** — cross-network via Slack | 🟡 Designed in two places; pre-implementation. The forcing function (mc-macbookpro EDR block) keeps this prioritized. | Medium — design fragmentation needs resolution |
| **Level 5** — public OSS | ✅ Aligned. CHANGELOG + tags discipline maintained. 5 collaborators in channel. Slack announcement pattern stable. | Low |
| **Level 6** — v1.0 rebrand (claude-mod) | ⏸ Deferred as planned. Brand decision locked at the right time horizon. | None |

**Net:** all goals are either on-track or correctly deferred. No goal is misaligned with current effort. The one goal-level investment Mitch should consider: **a small explicit experiment to exercise level 3 (team groups)** before v0.5 SlackConnector lands. Even just Atif and Shane joining a shared `branch-ai` group over LAN/VPN would validate the group-routing model with real users before adding the Slack transport on top.

---

## Recommendations (PM-level)

1. **Reconcile the two SlackConnector design docs this week.** Either fold tk421's annual-archives + re-embed-on-receive moves into the GitHub doc, or mark the tk421 draft superseded. Cost is small now, doubles every day.
2. **Cap v0.5 design work pending implementation.** Concretely: no further v0.5 design docs until WP-B + WP-D land. mc-macbookpro can drive WP-E/F detail design *only as questions arise during implementation*.
3. **Stage a level-3 dry run with Atif or Shane on LAN/VPN.** Even one shared group with three memories validates the team-routing path before Slack transport is added. Keeps users engaged; surfaces issues earlier.
4. **Land WP-α (per-peer per-kind cursor) as v0.4.1 ahead of v0.6.** It's a real correctness bug in shipped code; deferring it to v0.6 conflates two concerns. Could ship in 3-5 days as a focused bug fix.
5. **Add a periodic "mem-fusion usage drift check" on new installs.** Ask each new collaborator after a week if Claude is using mem-fusion unprompted. If not, the directive is decaying and needs reinforcement.

---

*Next update: when v0.5 connector-layer WPs (B/C/D) land, or in 7 days, whichever first.*
