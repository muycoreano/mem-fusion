# `docs/status/` — Project status reports

This directory holds two kinds of documents:

1. **Weekly dashboards** — `Dashboard_<YYYY-MM-DD>.md`. Auto-generated, fact-heavy, short. Tracks deltas week-over-week.
2. **Project updates** — `Project_Update_MemFusion_v<N>_<YYYY-MM-DD>_<HHMM>.md`. Hand-curated narrative reports, written ad-hoc when a milestone or pivot warrants it.

Dashboards are the heartbeat; project updates are the editorial.

---

## What the weekly dashboard tracks

Six metric groups, picked because each one has either a behavioural ratchet (velocity / quality) or an early-warning signal (adoption / risk).

### 1. Velocity

Reported per branch (`main`, `v0.5`), broken down by author.

- Commits in the period
- Files changed; lines added / removed
- Tags pushed
- CHANGELOG entries added under `[Unreleased]`
- Slack-announced ships (count of `chat.postMessage` from Mitch's Claude-MCP authored posts in `#branch-ai-mem-fusion`)

### 2. Work-package burndown

The current version's WP table re-rendered from the project update with status changes flagged.

- Counts by status: ✅ done, 🟡 partial / design, ⏸ not started, ❌ removed
- WPs that changed state in the period (new completions, new starts, removals)
- Critical-path WPs flagged

### 3. Adoption / engagement

Signals that mem-fusion is being used by people other than the author.

- `#branch-ai-mem-fusion` channel member count
- Channel members who posted in the period
- New install reports
- External bug reports turned into commits
- External contributions (PRs, issues, patches)

### 4. Quality

- Test invariant count (parsed from CHANGELOG and `tests/` directory)
- Open issues / open PRs (`gh issue list` / `gh pr list`)
- Test count trajectory vs last week

### 5. Goal alignment

The six-level scorecard from project update v1, restated with status:

| Level | Outcome | Status this week | Δ vs last week |
|---|---|---|---|
| 1 | Single-machine semantic memory | ✅ SHIPPED | — |
| 2 | Multi-machine personal sync | ✅ PROVEN | — |
| 3 | Multi-user team groups | 🟡 DESIGNED, not exercised | — |
| 4 | Cross-network team sync via Slack | 🟡 DESIGNED | — |
| 5 | Public OSS adoption flywheel | 🟢 ACTIVE | — |
| 6 | claude-mod rebrand at v1.0 | ⏸ DEFERRED | — |

### 6. Design ↔ implementation balance

A health signal: are we shipping code or just shipping docs?

- New design docs added in `docs/` this week
- New `src/` files / lines added
- Ratio of design-commits to implementation-commits

---

## Format conventions

- **Top of each dashboard is a 5-line scorecard.** Status traffic light + top three numbers + change vs last week. Anyone reading should know the state in 10 seconds.
- **All tables are fixed-width markdown.** Renders well on GitHub and pastes cleanly into Slack.
- **No risk register or open-questions sections** in the dashboard — those belong in narrative project updates. Dashboards stay factual.
- **Links over inlined content.** Each shipped commit links to its GitHub URL and its Slack announcement (if any).

---

## Cadence

**Generated every Friday at ~2:57 PM ET** (cron: `57 14 * * 5`), covering the trailing 7 days (the prior Saturday 00:00 → Friday 14:57 local).

Off-minute pick is deliberate — avoids piling on the API at the :00 mark and gives the post-commit cycle a few minutes of headroom before close-of-business.

If a week has nothing to report, the dashboard still ships with a `(no activity)` note for empty sections. Consistency of cadence > content volume — the value of the dashboard is the trend line, not any single snapshot.

---

## How it runs

### Data sources

| Source | Accessibility | Used for |
|---|---|---|
| `git log`, `git tag`, `git diff --stat` (local clone, `~/dev/mem-fusion`) | Local | Velocity (mirrored from GitHub) |
| `gh` CLI / GitHub API | Remote-accessible | Velocity, quality, contributions — primary truth source |
| Slack MCP (`mcp__claude_ai_Slack__*`) | Remote-accessible | Adoption, engagement |
| mem-fusion MCP (`mcp__mem-fusion__*`) | **Localhost only** — *not* reachable from a remote scheduled agent | Optional: memory-store meta-metrics |

### Execution modes

**Mode A — Remote scheduled agent  [ACTIVE].** A scheduled routine that authenticates to GitHub + Slack and writes a Dashboard file. Local mem-fusion stats are *omitted* (or pulled from a snapshot — see below).

**Mode B — Local on-demand.** Run from a Claude Code session on a peer with mem-fusion available. Identical to Mode A plus mem-fusion meta-metrics inlined.

**Mode C — Local cron companion (optional, deferred).** A `launchd` job on one peer drops a fresh `docs/status/.memory_snapshot.json` weekly. Mode A's remote agent reads that file from the repo when generating the dashboard so memory metrics are included without crossing the trust boundary.

**Current configuration:** Mode A weekly (Fri 14:57 ET). Mode B is always available on-demand from any peer. Mode C deferred until memory meta-metrics become a load-bearing part of the dashboard.

### Output

Each run writes `docs/status/Dashboard_<YYYY-MM-DD>.md` and commits it on `main` (or the active development branch) with the standard post-commit cycle: CHANGELOG entry → commit → push → optional Slack announcement.

---

## Generator prompt

The recurring agent receives this prompt verbatim (substitute current date):

> You are the mem-fusion weekly dashboard generator. Today is `<YYYY-MM-DD>`. The trailing week is the prior 7 days.
>
> 1. `cd ~/dev/mem-fusion && git fetch --all && git pull` (or remote: clone fresh into a tmp dir)
> 2. Collect velocity metrics: `git log --since=7.days --pretty=format:"%h|%an|%ad|%s" --date=short main v0.5`, `git diff --stat origin/main@{7.days.ago}..origin/main`, etc.
> 3. Read `#branch-ai-mem-fusion` (channel ID `C0B3MLAFNM8`) for the trailing 7 days via `slack_read_channel`. Count posts, identify external-user activity.
> 4. Read previous week's dashboard at `docs/status/Dashboard_<YYYY-MM-DD>.md` (if present) for week-over-week diffs.
> 5. Re-render the work-package table from the most recent `Project_Update_MemFusion_v*.md`, updating WP statuses based on CHANGELOG entries and commit history.
> 6. Compose the dashboard following the format in this README. Aim for ~1500 words.
> 7. Write to `docs/status/Dashboard_<YYYY-MM-DD>.md`.
> 8. Stage, commit (`docs: weekly dashboard <YYYY-MM-DD>` + Co-Authored-By trailer), push to origin.
> 9. Stop. Do not post to Slack without explicit user approval per the post-commit cycle rule.
