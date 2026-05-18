# Mem-Fusion — Website Mock

A single-file static landing page mock. Drop it in a browser to view; no build step, no dependencies.

```bash
open index.html
```

## What this is

A working-quality mock of how mem-fusion's marketing site could look. Single self-contained HTML file (~600 lines including embedded CSS, no JS, no external assets). Aligned with the canonical voice in [`../MESSAGING.md`](../MESSAGING.md).

## Design intent

- **Audience:** AI-forward users, not pure engineers. Reads more like Linear / Anthropic / Vercel than Qdrant / Pinecone — softer typography, generous spacing, capability-led copy, no terminal-coded dark accents.
- **Voice:** locked from MESSAGING.md. Possessive italics, triplets, plain English, no buzzwords. The frame is *"Claude is the most capable AI development environment ever shipped, but it needs two things..."* — capability-led, never deficit-led.
- **No "100x" or "revolutionary."** The audience aspiration is acknowledged in the closing section (*"Your AI should accumulate. Right now it doesn't."*) without using the buzzword itself.

## Section map

| # | Section | Purpose |
|---|---|---|
| 1 | Top bar | Sticky nav: How it works · Sharing · Community · GitHub · Install CTA |
| 2 | Hero | Primary headline + subline + two CTAs + badges (open source / MIT / MCP-native / local-first) |
| 3 | The frame | The capability-led intro from MESSAGING.md |
| 4 | Three pillars | Long-term memory · Local by default · Sharing built in |
| 5 | What changes | The "compounding work" bullets, framed as the AI promise finally landing |
| 6 | Day to day | The `/remember` example block from the README, set in a dark code panel |
| 7 | Two paths | Constellation (today) and Connectors (v0.5) side by side |
| 8 | How it installs | Three steps: open Claude / paste markdown / approve |
| 9 | Who it's for | Six audience cards — developer, eng team, consultant, researcher, PM, "anyone tired of re-prompting" |
| 10 | Open source | Eight resource cards: GitHub, Discussions, Issues, Changelog, Architecture, Constellation, Messaging, License |
| 11 | Closing | Repeat CTAs on dark background, restating the promise |
| 12 | Footer | Quick links and license note |

## What to iterate on next

- **Logo / wordmark.** Right now the wordmark is set in a system serif with a small accent dot. A real mark would replace it.
- **Hero visual.** The hero is type-only by design (modern AI startup pattern). A real site might add a subtle illustration or a "memory in motion" loop.
- **Color palette.** Currently warm off-white (`#FCFBF8`) + indigo accent (`#3B3FCF`). Approachable but distinct. Easy to retune from one CSS variable layer.
- **Real social proof.** Once there are stars, testimonials, or beta-user quotes, the "Who it's for" section should make room.
- **Demos beyond the `/remember` block.** A "memory in action" loop or short video showing the hook auto-capture flow would land harder than the static code panel.
- **Connectors page.** When v0.5 ships, the Connectors path needs its own page with the Slack-first story spelled out.
- **Constellation page.** Same — the peer mesh deserves its own deep-dive page once the audience starts asking.

## Files

- `index.html` — the page itself
- `README.md` — this file

## Authority

Anything in this mock that conflicts with [`../MESSAGING.md`](../MESSAGING.md) — the messaging guide wins. The mock is a render of the voice; the guide is the voice.
