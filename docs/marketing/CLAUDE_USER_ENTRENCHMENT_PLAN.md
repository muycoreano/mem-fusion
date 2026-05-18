# Mem-Fusion — Claude User Entrenchment Plan

**Audience:** mem-fusion contributors and the marketing-lane crew.
**Status:** v1 draft. Live document; updates land as commits.
**Voice:** strategic, not customer-facing. External-facing copy draws from [`MESSAGING.md`](MESSAGING.md).
**Drafted:** 2026-05-17. Migrated to repo: 2026-05-18.

---

## Strategic intent

**Go deep with the audience we already serve — Claude Code users — before going wide.**

Mem-Fusion is Claude-first by current implementation (hooks are Claude Code-specific; tools are MCP). MCP-portability gives us forward-compatibility, but our shipping product is Claude-coupled. Don't fight that — lean into it. Become *the* default memory layer for Claude Code users. Once we own that audience, breadth is a follow-on, not a precondition.

Architect is building the engineering moat (v0.5 connectors, v0.6 identity, the Crush-gbrain-and-Mem0 plan). This document is the audience-and-community moat that runs alongside it. The two reinforce each other: better product → easier audience capture; deeper audience → better product feedback.

---

## Who we serve, segmented

| Segment | Size estimate | What they need from us | How we reach them |
|---|---|---|---|
| **Power Claude Code users** (already use Claude Code daily for serious work) | Small but high-signal — likely tens of thousands globally, growing | Memory that just works; doesn't break their flow; respects their machine. Status as canonical add-on. | Anthropic Discord, Claude Code GitHub Discussions, r/ClaudeAI, AI engineering newsletters, MCP server directories. |
| **Indie AI-forward developers** (building solo or 2-person products with Claude) | Largest individual segment | A persistent assistant that knows their codebase. Trust that their data stays theirs. Easy install. | HN, r/LocalLLaMA, AI Engineer Discord, Twitter/X "AI building in public" crowd, Show HN posts. |
| **Small engineering teams** (2-10 engineers, Claude-curious) | Smaller count, higher conversion value | Multi-machine sharing. Internal-only collaboration. Story for "how do we use AI without giving up our data?" | Direct outreach via warm intros (Atif/Matt/Jake/Shane network → adjacent teams), small-team-focused content, sponsored tracks at AI Engineer events. |
| **AI-forward consultants** (multi-client engagements, want per-client memory boundaries) | Niche but high-signal — they tell stories | Per-client memory groups; portability across machines; story for "how do I keep Client A's stack out of Client B's session?" | Consultant newsletters (Hamel Husain, Eugene Yan, AI Engineering newsletters), case studies, Twitter/X. |
| **Researchers and analysts** (academic, R&D, financial analysts using AI for investigations) | Niche but interesting | Episodic memory of investigations; "what did we conclude last month?" recall. | Substack, ArXiv-adjacent communities, specialized Discords (alignment, mechanistic interp). |

**Priority order for the next 90 days:** Power users → indie developers → small teams. Consultants and researchers are validation cohorts; we serve them well but don't reorient the product to them yet.

---

## Where Claude users actually live (specific channels)

### Reach-and-resonance (broadcast)
- **Hacker News** — Show HN is the highest-leverage launch surface for this audience.
- **r/ClaudeAI** — most direct subreddit; active community of Claude Code users.
- **r/LocalLLaMA** — adjacent; local-first AI enthusiasts; *very* aligned with our local-by-architecture story.
- **r/MachineLearning** — broader but reachable with quality posts.
- **Lobsters** — smaller but high-quality for technical posts.

### High-signal individuals (relational)
- **Simon Willison** — most influential single voice on Claude Code + MCP. Worth real effort to engage.
- **Hamel Husain** — AI engineering practitioner; consultant audience.
- **Eugene Yan** — applied ML / LLM ops; engineering depth.
- **Mike Knoop, Sherman Leung, Latent Space hosts** — podcast surface for "memory as infrastructure" story.
- **Garry Tan** (gbrain) — the politically charged one. Treat as peer, not adversary. Engaging respectfully on the local-first thesis pays off.
- **Anthropic DevRel / Claude Code team** — direct relationship is gold. Atif may have a route here.

### Community substrates (ongoing presence)
- **Anthropic Discord** — official Claude community. Mem-Fusion should have a visible presence here.
- **MCP-related Discords** — modelcontextprotocol.io community, Anthropic MCP channel.
- **AI Engineer Discord** — Latent Space ecosystem.
- **r/ClaudeAI Discord** — community-run; less official, often more active.

### Aggregators (distribution surfaces)
- **modelcontextprotocol.io directory** — get listed.
- **awesome-mcp lists** on GitHub — get added.
- **awesome-claude-code** lists — get added.
- **AI engineering newsletters** (Latent Space, AI Engineer Roundup, AI Tidbits, Every) — get featured.
- **GitHub Trending** (organic; v0.5 release should attempt this).

### Conferences and events (medium-term)
- **AI Engineer World's Fair** (typically June each year — likely past for 2026 but on the calendar for 2027).
- **AI Engineer Summit NY** (October-ish).
- **MCP Day** (Anthropic-hosted, recurring).
- **Local AI / open-source AI meetups** in SF, NY, Seattle.

---

## Jobs-to-be-done framing

What Claude users hire mem-fusion to do:

1. **"Stop making me re-explain my project every morning."** — the cold-start fix.
2. **"Make my Claude on my laptop and my desktop agree on what they know."** — the multi-device fix.
3. **"Let my teammate's discovery reach my Claude without anyone writing a doc."** — the team fix.
4. **"Give me an assistant who knows my conventions, my style, my client's stack."** — the personalization fix.
5. **"Give me memory I actually control — not a SaaS that owns my data."** — the trust fix.

Every piece of content, every post, every demo should be answering at least one of these explicitly.

---

## 30-day plays (immediate, ship-this-month)

| Play | What it is | Who owns it | Success looks like |
|---|---|---|---|
| **A1. Submit to MCP directory** | Get mem-fusion listed in modelcontextprotocol.io's official server directory and major awesome-mcp lists. | Mitch / evangelist | Mem-Fusion appears when someone searches "memory" in MCP server lists. |
| **A2. Show HN launch (v0.5)** | Time a Show HN to v0.5 ship. Title and body pre-drafted per MESSAGING.md voice. Atif and Matt arranged to upvote within first hour. | Mitch + evangelist + beta team | Top 30 of HN front page. 200+ stars on repo in first 48h. |
| **A3. r/ClaudeAI launch post** | Same day as Show HN. Long-form post that *opens with a real-world workflow problem*, not a feature list. | Evangelist drafts; Mitch posts | 100+ upvotes; sustained comments answered for 48h. |
| **A4. Get Simon Willison's eyes on it** | Send personal note + the install demo. He covers what he respects; we make it easy to respect. | Mitch | At minimum, an honest read. At best, a write-up on his blog. |
| **A5. CLAUDE.md snippet pack** | A `/templates/` directory in the mem-fusion repo with ready-to-paste CLAUDE.md additions for common workflows (project-context priming, /remember habit setup, scheduled review templates). | Evangelist | 5-8 high-quality templates in repo, demonstrated in README. |
| **A6. Internal weekly demo at Branch** | 20-minute live demo at the all-hands or eng all-hands. Live show, not slides. | Mitch | At least 2 Branch teams (beyond eng) volunteer to try it. |

---

## 90-day plays (build relationships, content, distribution)

| Play | What it is | Success metric |
|---|---|---|
| **B1. Mem-Fusion office hours** | Weekly 30-min livestream / Discord-voice. Mitch + a beta user solve a real workflow problem on-stream. Recordings posted to YouTube. | 6-8 episodes shipped; 100+ recurring views per episode. |
| **B2. Three case studies, real users** | Atif (internal), Shane (internal), and one external indie dev — each with a "here's how mem-fusion changed my Claude workflow" story. Real screenshots, real metrics. | Three published case studies (blog or repo). |
| **B3. Latent Space podcast slot** | Pitch the show. Memory as infrastructure is a category-defining frame they'll bite on. | Booked appearance. |
| **B4. Connector ecosystem seed** | When v0.5 connectors ship, seed 2-3 community-contributed connectors (someone builds the Notion connector, someone builds the GDrive connector). Lowers the bar for ecosystem participation. | 2-3 PRs from non-Mitch contributors. |
| **B5. Comparison content (carefully)** | "Mem-Fusion vs. (the category)" — *not* attacking competitors; demonstrating where each fits. Use the four-property frame from MESSAGING.md. | One canonical comparison post that becomes the third-party reference. |
| **B6. Branch advocacy** | When Branch hires AI engineers, they hear about mem-fusion in onboarding. Branch's existence helps Mem-Fusion's credibility; Mem-Fusion's credibility helps Branch's recruiting. Symbiotic. | New Branch eng hires cite mem-fusion as part of why they joined. |

---

## 180-day plays (entrenchment)

| Play | What it is | Success metric |
|---|---|---|
| **C1. Mem-Fusion at a conference** | Talk submission to AI Engineer or MCP Day. Title candidates: *"Memory at the Edge: Local-First Architecture for AI Assistants"* / *"Consent as the Routing Primitive: Privacy in Agent Memory."* | Talk accepted, delivered, recorded, shared. |
| **C2. v0.6 identity launch as a moment** | Coordinated launch (blog + HN + podcast + Anthropic DevRel briefing). Identity is the v0.6 anchor and the architectural moat. | Identity becomes the talked-about feature in the category for a quarter. |
| **C3. Ecosystem map** | A canonical "AI memory landscape" page — capability matrix, links, where each tool fits. Editorially independent; benefits everyone but anchors the conversation around mem-fusion's frame. | Becomes a cited reference in third-party comparisons. |
| **C4. Branded developer day** | One-day in-person or virtual: speakers from beta-team and external contributors. Becomes the seed of an annual moment. | 50-100 attendees; sustained community momentum after. |
| **C5. OpenClaw integration** | Per architect's note, post-v0.6 we plug into OpenClaw as a memory layer. OpenClaw has serious sponsor weight (OpenAI, GitHub, NVIDIA, Vercel) — its user base becomes our distribution surface. | Working integration; documented co-marketing motion. |

---

## What we measure

**Vanity but useful:**
- GitHub stars (current baseline: ~unknown, growing).
- Repo clones / installs (proxied via install-doc views, smoke-test telemetry if we ever add it).
- Discord/community mentions.
- Press / podcast mentions.

**Real (the ones that matter):**
- Number of *active* beta users (people who have stored more than 10 memories AND used the system in the last 14 days).
- Number of teams running Constellation (the team-share use case).
- Number of community-contributed connectors / templates / CLAUDE.md snippets.
- Number of cited references in third-party comparison content.
- Cycle time on shipped features (the *real* compounding-AI metric — is mem-fusion actually making Branch's eng team faster?).

**Anti-metrics (don't optimize):**
- Vanity install count — we want *active* users, not "tried it once."
- Cloud users — we have none; we don't want them. Misaligned with positioning.
- Framework integrations — Mem0 owns SDK breadth; we own depth on Claude Code. Don't drift.

---

## Risks and watch items

1. **Anthropic ships native memory in Claude Code.** Likely within 12 months. Our consent-routed, peer-mesh, identity-baked architecture survives this; "Claude has memory" doesn't kill us because the *kind* of memory is the differentiator. But we need to be prepared with a "yes-and" position: *"Anthropic's native memory is the single-user, single-machine case. Mem-Fusion is the cross-device, cross-team case."*
2. **Mem0 pivots to local-first.** Unlikely (cloud is their revenue model) but possible. Response: lean harder on the consent-as-primitive frame, which they admitted is unsolved.
3. **gbrain adds multi-machine sync.** More plausible than Mem0 going local. Response: our P2P sharing protocol is the moat; their architecture (pgvector + git-markdown) makes P2P harder to graft on than for us to add typed-edges + Dream Cycle. We arrive at parity faster than they do.
4. **MCP loses momentum.** Unlikely given Anthropic's commitment, but if it stalls we need a portability story for at least one other runtime. v0.6 architect work should keep MCP-adjacent abstractions clean.
5. **Branch deprioritizes sponsorship.** Mitigation: case for sponsorship in `Executive_Brief_Mem_Fusion_v1_*.md`; make Mem-Fusion's value to Branch's own engineering measurable and visible.

---

## What's NOT in this plan (deliberate)

- **Cold paid marketing.** Wrong stage. Audience is small enough that earned attention beats paid attention.
- **Enterprise sales motion.** Too early. v0.6 identity is the foundation; enterprise requires a multi-quarter buildout.
- **Mobile or browser apps.** Out of scope. Claude Code is the surface.
- **Branch productization of mem-fusion as a product line.** Not yet. The strategic case for Branch sponsorship is in the brief; productization is a 2027 question.

---

## What happens next (this week)

1. Mitch reviews this plan; flags anything off-strategy.
2. Internal Slack post to #branch-ai-mem-fusion announcing the plan exists (and pointing at MESSAGING.md as the public artifact).
3. Evangelist drafts the Show HN body (held until v0.5 ships).
4. Daily intelligence cron runs at 08:03 local; first findings inform the next iteration of this plan.
5. Status file becomes `Claude_User_Entrenchment_Plan_v<N>_<date>_<time>.md` on the next material update.

---

## Authority

- **Public voice** lives in [`MESSAGING.md`](MESSAGING.md). Canonical for external copy.
- **Per-competitor talking points** live in [`COMPETITIVE_POSITIONING.md`](COMPETITIVE_POSITIONING.md). Sanitized for public-comms tone.
- **This plan** is the operational marketing strategy. Live document; updates land as commits with full git history.
- **The deeper architect-weakness analysis** stays local (adversarial-toned, not shaped for public reading).
