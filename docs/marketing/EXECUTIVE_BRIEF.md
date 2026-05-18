# Mem-Fusion — Executive Brief for Branch Leadership

**Audience:** Branch CEO and executive team; also useful as a public sponsor/contributor brief.
**From:** Mitch Coopet (CTO, Branch) — drafted with Claude-Marketing-Manager (tk421-iMac).
**Drafted:** 2026-05-17. Migrated to repo: 2026-05-18 (GitHub is the source of truth — see [`MESSAGING.md` § Slack post conventions](MESSAGING.md) and the routing-rule memory).
**Status:** v1 draft. Live document; updates land as commits.

---

## The 90-second version

- **Mem-Fusion is the long-term memory and team-sharing layer for Claude Code and other local AI tools.** Open source, MIT, MCP-native. Branch is the originating sponsor; Mitch is the maintainer.
- **The AI memory market is real and growing fast.** The category leader (Mem0) has ~47K GitHub stars; a YC-backed entrant (gbrain) launched April 2026 to ~16.6K stars in weeks. Everyone is converging on the same shelf — memory for AI agents.
- **Mem-Fusion holds a defensible position they don't:** local-first by architecture, peer-to-peer mesh across devices and teammates, consent as the routing primitive (not the identifier), and v0.6 identity baked in. Mem0 themselves named privacy and consent as the *open problem* in this category. Our architecture is the answer.
- **The vision is organization-scale: memory fused across an entire team's AI sessions, accelerating decisions with instant awareness of what colleagues have already figured out.** That changes how teams ship.

---

## What Mem-Fusion is, plainly

Claude Code is the most capable AI development environment shipping right now. The problem: out of the box, every session starts blank. Yesterday's architectural decision, last week's stated preference, last month's hard-won bug fix — all gone. Multiply that across a team and across devices, and most of the value an AI assistant could deliver is being lost to forgetting.

Mem-Fusion fixes this by adding two missing pieces to Claude:

1. **Long-term memory.** Hooks watch your Claude sessions and quietly capture the decisions, preferences, and code patterns you produce. The next session starts already knowing them. The longer you use it, the sharper your Claude gets at *your* work.

2. **Peer-to-peer sharing.** When you're ready, that memory flows across your other machines or your teammates' machines — directly, peer-to-peer, with no server in the middle and no SaaS provider in the loop.

Everything runs locally. Privacy is enforced by the architecture, not by a policy on a website.

---

## The market we're in

The agentic AI category is consolidating around three layers: the model (Anthropic, OpenAI), the runtime (Claude Code, Cursor, OpenClaw), and **memory**. The memory layer is the least-settled of the three, and the one where local-first / privacy-respecting positions are most defensible.

Three serious entrants, each making a different bet:

| Competitor | What they bet on | Where it leaves them |
|---|---|---|
| **Mem0** (~47K stars, Apache-2.0) | Cloud-hosted memory-as-a-service. 21+ SDKs. Framework breadth. | Strong on scale; admits in their own *State of Agent Memory 2026* report that privacy and consent are *the* unsolved problem in this category. |
| **gbrain** (~16.6K stars, MIT, YC-backed) | Single-user local knowledge graph with typed edges and overnight consolidation. Polished. | Architecturally elegant for *one* person on *one* machine; no multi-device sync, no peer sharing, no team story. |
| **Letta** (Apache-2.0, MemGPT successor) | OS-style memory tiering — RAM-like working memory + archival. Stateful-agent abstraction. Cloud-centralized. | Different design philosophy. No personal mesh; no consent-routed sharing. |

The category leader has stars and SaaS revenue. The architectural neighbor has design polish. Neither has built what Mem-Fusion has built.

---

## Where Mem-Fusion wins (the four-property claim)

No other memory layer in the category holds all four of these together:

1. **Local by default.** Data stays on your machine. *Private by architecture*, not by policy.
2. **Personal mesh.** Your laptop, desktop, work machine, and teammates share memory directly. No server in the middle, no cloud provider in the loop.
3. **Consent as the routing primitive.** Every memory carries an explicit group tag. Sharing happens by named intent — never by a content classifier's best guess.
4. **Identity baked in (v0.6, in flight).** Every memory remembers *whose* perspective it came from. Across machines. Across sessions. Across teammates.

Each competitor holds one or two of these. We hold all four. The intersection is the defensible position.

---

## The vision (the bigger story)

Mem-Fusion today is a memory layer for one developer and a small team. The vision is bigger:

> **Memory fused across an entire organization, surfacing what colleagues have already figured out the moment it becomes relevant — so decisions happen faster, with everyone working from the same lived institutional knowledge.**

Imagine a Branch sales team where every conversation, every objection handled, every won deal compounds into the next call — not as a document someone has to remember to read, but as context that surfaces the second it's relevant. Imagine an engineering org where the production incident at 11pm is in every on-call's Claude by 9am, without anyone writing a post-mortem. That's where this leads.

We're not there yet. v0.4 is shipping; v0.5 connectors and v0.6 identity are in design. But the architecture is built around the right primitives to get there.

---

## The next 90 days

| When | Milestone | What it unlocks |
|---|---|---|
| **By 2026-05-31** | Ship v0.4.1 (cursor bug fix + 8000-char embed guard). | Internal beta team (Atif, Matt, Jake, Shane, John Dwight) on stable footing. |
| **By 2026-06-15** | Ship v0.5 (connectors framework — Slack first). | Memory flows through Slack channels Branch teams already use. No new infra to adopt. |
| **By 2026-07-15** | Ship v0.6 (identity / `about_me`). | Each memory knows whose perspective it represents. Foundation for org-scale fusion. |
| **Ongoing** | Daily intelligence brief, weekly Branch internal demo, monthly external community push. | Stays ahead of category shifts; builds Branch's profile in AI engineering. |

---

## Why this matters to Branch (the strategic case)

Mem-Fusion is open source and MIT-licensed. Branch doesn't make money on it directly. So why sponsor it?

1. **Brand and talent gravity.** Open-source AI infrastructure projects are a category-defining hire magnet right now. Mem-Fusion's public traction (already collaborating with Atif Siddiqi, Matt Peterson, Jake Drost, Shane Pitts internally; positioned to expand to AI-engineering circles externally) signals Branch as a serious AI-forward company. Recruiting cost savings alone can justify the sponsorship.

2. **Internal leverage.** Branch's own engineering and product teams using mem-fusion compound their AI investment day over day. The smarter Claude gets at the Branch codebase, the worker-payments domain, the Stripe partnership constraints — the more leveraged every IC's time becomes. This is measurable in cycle-time on shipped features.

3. **Forward option on memory-as-infrastructure.** If AI memory becomes infrastructure (likely), being the maintainer of the local-first / consent-routed reference implementation is a non-trivial position. Branch sponsoring it now is cheap; trying to acquire equivalent positioning in 18 months will be expensive.

4. **Customer-facing potential.** Branch's worker-facing products could embed mem-fusion-class capabilities — a worker advisor that remembers stated preferences, a payroll admin's AI assistant that knows last quarter's reconciliation choices. The architecture is built for this; the integration is plausible post-v0.6.

5. **Privacy story.** Branch handles financial data for workers. Local-first memory architecture is a strategically aligned message — "we don't trust your data to cloud vendors" is the same posture Branch already takes on worker financial records. Mem-Fusion is the engineering exemplar of that value.

---

## Asks

Three concrete asks for executive support:

1. **Sustained sponsorship of Mitch's mem-fusion time.** Current pace (designing v0.5, working with internal beta, shipping releases on the v0.5 branch) requires consistent allocation. Asking for explicit endorsement, not just toleration.

2. **Internal distribution help.** Permission to formally pitch mem-fusion to additional Branch teams (sales engineering, product, customer support) for internal beta. Each adopting team compounds Branch's leverage and generates the case studies we'll use externally.

3. **One external amplification moment.** When v0.5 ships, a short Branch-leadership-authored post (or repost / co-sign) on LinkedIn or Branch's blog. The category is small enough that a single executive co-sign from a real AI-forward fintech moves the needle on third-party trust.

Nothing here requires new spend. The ask is alignment, permission, and one short post per release.

---

## Reading list (in priority order)

- [`MESSAGING.md`](MESSAGING.md) — public voice + Why Mem-Fusion stance.
- [`COMPETITIVE_POSITIONING.md`](COMPETITIVE_POSITIONING.md) — per-competitor talking points for sales/community conversations.
- [`CLAUDE_USER_ENTRENCHMENT_PLAN.md`](CLAUDE_USER_ENTRENCHMENT_PLAN.md) — the operational marketing plan (segments, channels, 30/90/180-day plays).
- [`../v1.0-05182026-ROADMAP.md`](../v1.0-05182026-ROADMAP.md) — unified product roadmap v0.3 → v0.7.
- [`../status/`](../status/) — auto-generated weekly dashboards + hand-curated project updates (latest PM-level health check lives here).

---

## Status

Draft v1. Ready for review by Branch leadership. Updates land as new files (`Executive_Brief_Mem_Fusion_v<N>_<date>_<time>.md`) per the convention used for project updates.
