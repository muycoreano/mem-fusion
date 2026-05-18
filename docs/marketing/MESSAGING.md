# Mem-Fusion — Messaging Guide

This file is the canonical voice and positioning for Mem-Fusion. Anything that goes out under the Mem-Fusion name — homepage, README, social posts, conference talks, sales pitches, Slack announcements, demos — should align with what's written here.

If you find yourself reaching for hype words ("revolutionary," "100x," "next-gen") or a different frame ("fixes Claude's biggest flaw"), come back here first.

---

## The frame

Claude Code is the most capable AI development environment ever shipped. To make it the way a team actually builds — across machines, across teammates, across weeks of work — it needs two things that don't come in the box: **long-term memory** and **the ability to share what it learns**. Mem-Fusion is the layer that adds both.

We do not position Mem-Fusion as a patch for Claude. We position it as the layer that graduates Claude into the team's real tool.

---

## Headlines

| Surface | Headline | Use when |
|---|---|---|
| **Primary** | **Make Claude the tool your team builds with.** | Homepage hero, decks, conference titles, anywhere the reader is the hero. |
| **GitHub tagline** | **Mem-Fusion turns Claude into your team's AI.** | GitHub one-liner, Show HN headlines, anywhere the product is the hero. |

Sublines that pair with either:

- *Long-term memory and team sharing for Claude — local by default.*
- *Memory and sharing for Claude. Local, peer-to-peer, MIT.*

---

## The three pillars

1. **Long-term memory.** Four Claude Code hooks watch your sessions and capture decisions, errors, and code patterns automatically. The longer you use it, the sharper Claude gets at *your* work, in *your* codebase, with *your* preferences.

2. **Local by default.** Qdrant + Ollama on your machine. No cloud account, no telemetry, no SaaS lock-in. Your data is yours — the architecture enforces it, not a policy.

3. **Sharing built in.** Two paths. **Constellation** pools memory across your machines and teammates' machines directly — peer mesh, no server in the middle. **Connectors** (v0.5) bridge into productivity tools your team already uses — Slack first, GDrive and Teams next. Run neither, one, or both.

---

## What we unlock (the opportunity, not the pain)

- The work you do Monday compounds into Tuesday. Every session adds to the next.
- Your laptop, your desktop, and your work machine all build on the same Claude.
- Your teammate's 11pm breakthrough is in your Claude at 9am — without anyone writing a doc.
- Claude graduates from *the brilliant assistant you retrain every morning* to *the colleague who already knows the codebase.*

---

## Audience promises

| Audience | What we promise |
|---|---|
| Solo developer | Claude knows your codebase across every machine you work on. Past decisions, conventions, patterns — they persist. |
| Engineering team | Your teammate's discoveries reach everyone's Claude — automatically. Without writing docs. |
| Consultant | Per-client memory groups. Client A's stack never leaks into Client B's session. |
| Researcher / analyst | Claude can answer *"what did we conclude last month?"* — because it actually remembers. |
| PM / professional | Stop onboarding Claude every morning. Decisions, preferences, project context persist. |

---

## Anti-positioning (what we are not)

- **Not a cloud service.** No account, no servers we run, no per-seat pricing.
- **Not a content classifier guessing what's safe to share.** The user tags every memory; routing is deterministic.
- **Not a Claude replacement.** An extension of it.
- **Not Claude-only forever.** Built on MCP. Claude first, not Claude only.

---

## Voice principles

- **Possessive italics.** *your* work, *your* codebase, *your* preferences, *your* team. Quiet ownership; never a hard sell.
- **Lead with what's possible.** *"Claude becomes the colleague who remembers"* beats *"Claude forgets."* Capability, not deficit.
- **Triplets for accumulation.** *"Across machines, across teammates, across weeks of work."*
- **Architecture as evidence.** *"The architecture enforces it"* beats *"we promise privacy."* Show, don't pledge.
- **Plain English with one technical word per paragraph.** Engineers respect the technical word; professionals skip past it without losing the thread.
- **Never lead with the tool surface.** *"12 MCP tools"* is a feature list, not a reason to care.

---

## Three messages every piece of copy must carry one of

1. **Memory.** Claude that remembers what you've decided, learned, built.
2. **Local.** On your machine. Yours.
3. **Shared.** Across your devices. Across your team.

If a piece of copy carries none of these, it's not on-message.

---

## What we don't say

- *"Claude is broken."* (We respect the tool we extend.)
- *"Revolutionary," "100x," "next-gen," "game-changing."* (The product is concrete; the copy stays concrete.)
- *"Disrupts X."* (We don't talk about competitors in our own copy.)
- *"AI memory platform."* (Generic. Anyone could say it. We say *"the memory and sharing layer for Claude Code."*)
- *"Trust us with your data."* (We don't ask for trust. The architecture means we don't need it.)

---

## Collateral templates

### Homepage hero (≈ 120 words)

> **Make Claude the tool your team builds with.**
>
> Claude Code is the most capable AI development environment ever shipped. To make it the way your team actually builds — across machines, across teammates, across weeks of work — it needs two things that don't come in the box: long-term memory and the ability to share what it learns.
>
> Mem-Fusion adds both. A local memory layer watches your Claude Code sessions, captures decisions and patterns as you work, and surfaces them the next time they're relevant. The longer you use it, the sharper your Claude gets at *your* work, in *your* codebase, with *your* preferences.
>
> When you're ready, that memory flows across your other devices and your teammates — directly, peer-to-peer, no server in the middle.
>
> **Everything runs on your machine. Nothing leaves it without your say-so.**

### README opening

> # Mem-Fusion
>
> **Long-term memory and sharing for Claude Code — the layer that makes it a real tool for real teams.**
>
> Claude Code is the most capable AI development environment ever shipped. To make it the way your team actually builds — across machines, across teammates, across weeks — it needs two things that don't come in the box: long-term memory and the ability to share what it learns.
>
> Mem-Fusion is the layer that adds both. A local memory system watches your sessions, captures decisions and patterns as you work, and surfaces them automatically the next time they're relevant. The longer you use it, the sharper your Claude gets at *your* work, in *your* codebase, with *your* preferences. Everything runs on your machine; nothing leaves it.
>
> When you're ready for memory to flow across your devices or your teammates, Mem-Fusion has two paths: **Constellation**, a peer mesh for direct machine-to-machine sync (no server in the middle), and **Connectors** (v0.5), which bridge into productivity tools your team already uses — Slack first, GDrive/Teams/Notion next. Run neither, one, or both.

### Hacker News / Show HN

**Title:** *Mem-Fusion: long-term memory and team sharing for Claude Code — local, MCP-native*

**Body opening:**
> Claude Code is capable enough to be a serious tool, but two things keep most teams from making it part of how they actually build: it has no long-term memory, and what one Claude figures out doesn't reach any other Claude. Mem-Fusion is the open-source layer that adds both — MIT-licensed, MCP-native, and entirely local (Qdrant + Ollama on your machine). The optional sibling, Constellation, pools memory across teammates' machines via peer-to-peer HTTP. No orchestrator, no central service.

### Tweet (≤ 280 chars)

> The two things keeping Claude Code from being your team's actual tool: it doesn't remember, and one Claude can't share what it learned with another.
>
> Mem-Fusion adds both. Open-source. Local. No cloud.
>
> github.com/muycoreano/mem-fusion

### Slack one-liner (when someone asks "what's mem-fusion?")

> Mem-Fusion is the memory and sharing layer that makes Claude Code something your team actually builds with. It captures what you and Claude figure out so the next session doesn't start blank — and lets that memory flow across your devices and teammates without going through a cloud. Open-source.

---

## Why Mem-Fusion (the public stance)

The AI memory category has serious entrants now. Most are good at one or two things. Mem-Fusion is built around **four** properties that — taken together — no other memory layer holds:

1. **Local by default.** Your machine, your data. Not *private by policy* — private by architecture.
2. **A personal mesh.** Your laptop, your desktop, your work machine, and your teammates share memory directly. No server in the middle.
3. **Consent as the routing primitive.** Every memory carries an explicit `groups` tag. Sharing happens by named intent — never by a classifier guess.
4. **Identity baked in (v0.6).** Each memory knows whose perspective it came from — across machines, across sessions, across teammates.

The intersection is the point. Other layers hold one or two of these. Mem-Fusion holds all four.

### On the broader category (mutual elevation, not attack)

The category leader, **Mem0**, named privacy and consent as *the open problem* in agent memory in their [2026 State of Agent Memory report](https://mem0.ai/blog/state-of-ai-agent-memory-2026). We agree — and Mem-Fusion's architecture is built to answer it. Their model uses identifier scopes (`user_id` / `agent_id` / `org_id`) — those describe *who's involved*. Our model uses group-keyed-by-explicit-intent — that describes *who consents to see this*. The two aren't the same, and the difference is the whole story.

We don't compete on framework breadth or scale-out SaaS — those are different products for different jobs. We compete on this:

> *The memory layer that makes Claude part of how your team actually builds — with privacy that survives contact with reality.*

### What we say when asked "how is this different?"

Always lead with what we are, never with what they aren't. Examples:

- *"Mem-Fusion is the local-first, peer-shared memory layer. Other excellent products in this space are cloud-hosted or single-machine — we're the one built for the multi-device, multi-teammate case where data stays on your machines."*
- *"Where most memory tools describe who's involved, Mem-Fusion's architecture describes who consents. That's the privacy invariant the category leader has called the open problem — and the reason we exist."*

What we don't say: anything attacking a specific competitor by name, anything that reduces another tool to its weakest dimension, anything that positions us as a patch on someone else's mistake.

---

## Slack post conventions

Anything we send under the Mem-Fusion banner — to `#branch-ai-mem-fusion`, the public Mem-Fusion announcement channel, or any future team Slack — follows this format. Apply across roles (Claude-Architect, Claude-Engineer, Claude-Project-Manager, Claude-Marketing-Manager) and across machines. Routine and one-off posts both. Established 2026-05-18.

### Shape

1. **Backtick lede** — the title, wrapped in backticks. Acts as the headline.
2. **Bold value sentence** — one or two lines stating the takeaway. The reader who sees only the lede and the value line should still know what happened.
3. **Body** — fixed-width tables for any file/change or comparison content; standard markdown otherwise (`**bold**`, `*italic*`, `> blockquote`, fenced code, links).
4. **Sign-off** — the role hat currently writing: `Claude-Architect`, `Claude-Engineer`, `Claude-Project-Manager`, or `Claude-Marketing-Manager`. Role tags are facets of one CTO identity for cross-machine attribution; the tag clarifies which hat is writing, not who is writing.
5. **Closing tagline** — every post ends with:

   ```
   Memorized and shared with mem-fusion: <memory-id>
   ```

   The `<memory-id>` is the UUID of the memory record containing the post's source content. The tagline is how readers retrieve full context later — anyone with Mem-Fusion installed can `search_memory` or `export_record` the id and reconstruct what we knew at post time. Use exactly that wording. No emoji prefix. Backtick the id or leave it plain at the author's discretion.

### Never include

- **Links to local filesystem paths.** Anything like `~/CTO Assistant/...`, `/Users/<anyone>/...`, or `/home/<anyone>/...` is unreachable to the channel and leaks per-machine context. Use GitHub URLs, public blog URLs, or the canonical tagline above. If the source lives only on a local machine, the memory id is the access surface — not the path.
- **Local-only commit hashes without a URL.** Always link the commit (`https://github.com/muycoreano/mem-fusion/commit/<sha>`), not just the SHA.
- **Sensitive info in URL query params.**

### Markdown dialect

The Slack MCP accepts **standard markdown** (`**bold**` with double asterisks), not Slack mrkdwn (`*bold*` with single asterisks). The MCP layer translates. Draft for standard markdown.

### Approval gate (cycle-dependent)

- **Routine autonomous posts** (daily intel, scheduled status dashboards, scheduled summaries) post directly from the cron / scheduled-agent flow. No human approval gate inline.
- **Non-routine posts** (releases, design announcements, corrective memories that touch external comms) follow the ship+announce cycle: draft → wait for explicit user approval → post.

When in doubt, draft and wait. The cost of pausing to confirm is small; the cost of an unintended team post is large.

### Examples

A release announcement might look like:

```
`v0.5.1 ships — connector cursor persistence + content_hash canonical form`

**Engineer-lane release. Stage 1.5 of the v0.5 plan landed in 4 commits over 36 hours. No breaking changes; existing memories preserved.**

[body with file/change table, GitHub commit link, etc.]

Memorized and shared with mem-fusion: `<uuid>`

— Claude-Engineer (mc-macbookair)
```

A daily-intel post (routine, no approval gate):

```
`Daily intel — 2026-05-18`

**Cloudflare Agent Memory (Apr 17 beta) now publicly markets team-shared memory profiles. The middle pole (local + mesh + consent) is more defensible than ever.**

[headline signal · category map · threats · hard question · this-week ask]

Memorized and shared with mem-fusion: `<uuid>`

— Claude-Product-Manager (tk421-iMac, daily-intel run)
```

---

## Authority and updates

This file is the canonical messaging source. To propose a change:

1. Open a PR against this file.
2. Show the message you want to send (homepage copy, talk title, etc.) and which guidance it would violate or refine.
3. Get one other contributor's review before merging — voice consistency is the whole point of having this doc.

Last locked: 2026-05-17.
Last extended: 2026-05-17 (Why Mem-Fusion section added).
Last extended: 2026-05-18 (Slack post conventions section added).
