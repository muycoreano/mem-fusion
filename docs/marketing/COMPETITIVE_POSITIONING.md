# Mem-Fusion — Competitive Positioning Brief

**Audience:** mem-fusion contributors and anyone speaking on behalf of the project (customer calls, podcasts, conference Q&A, GitHub issue replies). Sanitized to fit our public-comms posture from day one — no adversarial framing here, just the per-competitor talking points and the mutually-elevating moves.

**Purpose:** the canonical *"how do we talk about competitors"* reference. Use this when a customer, beta tester, journalist, or open-source contributor asks how Mem-Fusion compares to gbrain, Mem0, Letta, or anyone else. The voice rules from [`MESSAGING.md`](MESSAGING.md) apply throughout: capability-led, possessive italics, no attacks, no buzzwords.

The public-facing short version of this content lives in [`MESSAGING.md`](MESSAGING.md) under *Why Mem-Fusion*. This document gives contributors the deeper talking points behind that section.

**Status:** v1. Live document; updates land as commits. Migrated to repo 2026-05-18 — GitHub is the source of truth.

---

## The four-property white-space claim (load-bearing)

No other memory layer in this category holds all four of these:

| # | Property | Why it matters |
|---|---|---|
| 1 | **Local by default** | Data stays on your machine; not "private by policy" — private by architecture. |
| 2 | **Personal mesh** | P2P sharing across your devices and teammates without a server. |
| 3 | **Consent as routing primitive** | Every memory carries an explicit `groups` tag. Routing is by named intent, not classifier guess. |
| 4 | **Identity baked in (v0.6)** | Each memory knows whose perspective it came from. Cross-machine, cross-session. |

When someone asks *"why mem-fusion?"* — anchor here. The competitors each hold one or two; we hold all four together.

---

## Per-competitor cribsheet

### gbrain (Garry Tan / YC, MIT, ~16.6K stars, launched April 2026)

**What gbrain does well:** local-first; hybrid pgvector + git-markdown; auto-extracted typed knowledge-graph edges (pattern-based, no LLM calls — works_at, invested_in, founded); overnight "Dream Cycle" consolidation (11 phases: lint → backlinks → consolidate → embed → orphans). Excellent for a single user's personal knowledge graph. Architecturally our closest neighbor.

**Where we diverge:**
- gbrain is **single-machine first.** It has no multi-device sync story, no peer mesh, no group-keyed sharing. Built for the individual's "second brain" — not for memory that flows across a team or across devices.
- gbrain has **typed graph edges and Dream Cycle consolidation** that we don't have yet (v0.7+ candidates).
- We have **the P2P sharing protocol, explicit consent routing, and identity model** — gbrain doesn't.

**When someone asks "isn't this just gbrain?":**
> *"gbrain is excellent for a single user's personal knowledge graph — typed edges, overnight consolidation, polished. Mem-Fusion is built for the case where memory needs to flow: across your devices, across your team, and through the productivity tools you already use. Different scope, same respect for local-first."*

**What NOT to say:** *"gbrain doesn't have X"* (negative framing). *"gbrain is just for solo developers"* (reductive — they're more than that). *"We're like gbrain but better"* (we're different, not better).

**Gap items we should ship eventually (acknowledge if pressed):**
- Typed knowledge-graph edges — v0.7+ candidate. Additive to our local-first / P2P / identity story.
- Overnight consolidation pass — v0.7+ candidate, async batch job.

---

### Mem0 (Apache-2.0, ~47K stars, the category giant)

**What Mem0 does well:** scale, community, framework breadth (21+ SDK integrations), hybrid vector+graph+KV, cloud-hosted SaaS for production agent deployments. "Actor-aware" attribution within a session. They are the volume leader of agent-memory by an order of magnitude.

**The framing leverage (load-bearing):**
Mem0 published in their [2026 State of Agent Memory report](https://mem0.ai/blog/state-of-ai-agent-memory-2026) that **privacy and consent are *the* unsolved problem in agent memory.** Quote-and-cite is the play.

Their model uses identifier scopes — `user_id`, `agent_id`, `org_id`. Those describe *who's involved*. Mem-Fusion uses group-keyed-by-explicit-intent. That describes *who consents to see this*. They aren't the same, and the difference is the whole story.

**Where we diverge:**
- Mem0 is **cloud-first.** Production Mem0 is a hosted service. Local-only is a different product path.
- Mem0 is **single-tenant by identifier**, not multi-tenant by consent. Identifier scopes describe involvement; group routing describes permission.
- Mem0's "actor-aware" is **in-session only.** Our v0.6 identity model is cross-machine, cross-session.
- Mem0 has **21+ SDKs and broad framework integration.** We have one MCP server and Claude Code hooks. Different bet.

**When someone asks "why not just use Mem0?":**
> *"Mem0 is excellent for production agent deployments at cloud scale — their framework breadth and scale-out story are genuine strengths. Mem-Fusion is built for the case where data stays on your machines and consent is the routing primitive, not the identifier. Mem0 themselves named privacy and consent as the open problem in this category. That's the problem we built the architecture to solve."*

**The killer quote-and-cite (use when appropriate):**
> *"Mem0's 2026 State of Agent Memory report names privacy and consent as the open problem in agent memory. Mem-Fusion's group-keyed-by-explicit-intent design is built specifically to answer it."*

**What NOT to say:** anything attacking Mem0 directly. Anything reducing Mem0 to "cloud = bad." Anything framing this as adversarial. They named the problem; we built the answer. Mutually elevating.

---

### Letta (MemGPT successor, Apache-2.0)

**What Letta does well:** stateful agents that "continually learn." OS-style memory tiering (RAM-like working memory + archival memory). Strong agent-framework story. Self-hostable, with cloud option.

**Where we diverge:**
- Letta is **cloud-centralized** for production use; local CLI exists but the platform is the cloud.
- Letta has **no personal mesh.** Memory belongs to an agent; teammates' agents don't share memory directly.
- Letta is **OS-style tiering** (RAM/archival). We are **deliberately off-strategy on this** — single Qdrant collection, simpler model.

**When someone asks "what about Letta?":**
> *"Letta is built around the stateful-agent abstraction with OS-style memory tiering — that's a different design than ours. Mem-Fusion is built for the persistent, local, peer-shared case where the human is the principal and the agent is the assistant. We don't try to be a stateful-agent platform."*

**What NOT to say:** *"Letta is too complicated."* (Patronizing.) *"Tiering is the wrong model."* (Stay agnostic — they made a deliberate choice.)

---

### OpenClaw AI (Peter Steinberger, MIT, early 2026)

**Critical reframing:** OpenClaw is **NOT a competitor.** It's an agent runtime that needs a memory layer. Currently uses MEMORY.md files (no vector store, no MCP integration). Sponsors include OpenAI, GitHub, NVIDIA, Vercel — significant user base.

**Strategic implication:** OpenClaw is a **distribution channel** for Mem-Fusion, not a head-to-head competition. The integration looks like: Mem-Fusion exposes its MCP tools to OpenClaw's tool router; or a thin OpenClaw-shaped adapter that maps OpenClaw's MEMORY.md update calls into Mem-Fusion store_memory calls.

**When someone asks "how does this work with OpenClaw?":**
> *"OpenClaw is an agent runtime; Mem-Fusion is the memory layer. They're complementary. We're scoping Mem-Fusion as a memory plugin for OpenClaw post-v0.6 — OpenClaw users today get markdown-file memory; we offer them vector + local-first + P2P sync as an upgrade path."*

**What NOT to say:** anything that treats OpenClaw as a competitor in any external communication. Address it as a complementary host platform.

**Priority:** scope after v0.6 lands.

---

## The "Why Mem-Fusion" 30-second pitch

Use this when someone asks for the one-paragraph version:

> Most memory layers for AI are either single-machine personal notes or cloud-hosted services that own your data. Mem-Fusion is the one built for the gap in between — long-term memory that lives on your machines, flows directly to your other devices and your teammates' machines in a peer mesh, and treats consent as the routing primitive, not as a policy. The category leader publicly named privacy and consent as the open problem in agent memory. Our architecture is the answer.

---

## Three strategic moves (from architect, 2026-05-17)

1. **Communicate the privacy/consent answer publicly.** Mem0's framing is the gift — *"they identified the problem; we built the answer."* Use the citation. Mutually elevating, never adversarial.

2. **Ship v0.6 cleanly.** The about_me identity model establishes a moat hard to fast-follow because it touches schema + hook lifecycle + sync protocol simultaneously. Competitors can't trivially graft it on.

3. **Position Mem-Fusion as the memory layer for OpenClaw users.** Distribution channel, not competition. Post-v0.6 work.

---

## What's explicitly off-strategy (do not chase)

- **OS-style memory tiering** (Letta's bet). We have one Qdrant collection. Simpler model is the choice.
- **Cloud SaaS offering.** Local-by-architecture is the moat. Going cloud erodes it.
- **Framework breadth at 21+ SDKs** (Mem0's bet). One MCP server and Claude Code hooks is the focus. Claude first, MCP-portable, not framework-exhaustive.

If a customer asks for one of these, the answer is *"that's a different product. Here's where Mem-Fusion fits, and here's the conversation worth having about your real problem."*

---

## Citations and references

- Mem0 2026 State of Agent Memory: https://mem0.ai/blog/state-of-ai-agent-memory-2026
- gbrain repo: github.com/[Garry Tan's org]/gbrain (~16.6K stars as of 2026-05)
- Letta repo: github.com/letta-ai/letta
- OpenClaw: openclaw.ai, github.com/openclaw/openclaw, github.com/ComposioHQ/secure-openclaw
- Full architect analysis: a deeper competitor-weakness analysis is kept local on the architect's machine (not in this repo, by deliberate scope — it names specific user pain in competitor products and isn't shaped for public-facing comms). This doc is the sanitized, mutually-elevating version that contributors can reference openly.

---

## Update protocol

When new competitors or category shifts emerge:
- Architect adds findings via mem-fusion memory + a PR against this doc.
- Marketing-lane updates open a PR against [`MESSAGING.md`](MESSAGING.md) (canonical voice) or this doc (per-competitor specifics), as appropriate.
- This is a live document; iterate in-place. Significant re-shapes can land as `v2`, `v3` etc. via a new file alongside this one if the history would otherwise be lost — but default is in-place edits with git history as the record.
- The deeper architect-weakness analysis stays local (it's adversarial-toned and not shaped for public reading); this doc carries the mutually-elevating distillation of it.
