# mem-fusion Weekly Dashboard — 2026-05-15

**Period:** 2026-05-08 → 2026-05-15 (trailing 7 days)
**Baseline:** this is the first dashboard; no prior period to diff against. Future dashboards report week-over-week deltas.
**Generated:** 2026-05-15 (manual; first scheduled run will be the following Monday)

---

## Scorecard

```
Status:        🟢 GREEN
Velocity:      37 commits on main, 6 unique to v0.5      (Δ N/A — baseline)
Quality:       202 test invariants, 0 open issues / PRs  (Δ N/A — baseline)
Adoption:      6 humans in channel, 3 external posters   (Δ N/A — baseline)
Top movement:  v0.4.0 effectively shipped; v0.5 opened with WP-1, WP-4, WP-J + Slack connector design
```

---

## 1. Velocity

### Commits by branch (trailing 7 days)

```
Branch    Commits   Files changed   +Lines    -Lines
────────  ────────  ──────────────  ────────  ────────
main      37        51              +15,180   -1,486
v0.5      6 unique  7               +820      -10
TOTAL     43        58              +16,000   -1,496
```

### Author breakdown

```
Author          Commits   Notes
──────────────  ────────  ──────────────────────────────────
Mitch Coopet    42        Sole committer; one alias "M C" (1 commit, same person)
External        0         Atif's hook bug catch is credited in commit message but applied by Mitch
```

### Shipped-commit announcements in Slack

Mitch posted **7 ship-summaries** to `#branch-ai-mem-fusion` in the period (Claude-MCP authored, fixed-width file/change tables):

```
Date         Commit           Summary
───────────  ──────────────   ──────────────────────────────────────────────────────────────────
2026-05-14   a86b32f          Smoke-test e2e coverage; env-configurable QDRANT_URL/OLLAMA_URL
2026-05-14   1be857b          --scope user MCP registration fix (loads from any cwd)
2026-05-14   067f6c0          Cold-start fix: auto-merge CLAUDE.md, preload 8 seeds, import file memories
2026-05-14   (—)              Hook attribute fix (Atif); tests 171 → 202
2026-05-15   c6839a2          v0.5 opens: /remember push privacy invariant + fix-scripts pattern
2026-05-15   a40e858          docs/v0.5_SLACK_CONNECTOR.md design doc on v0.5 branch
2026-05-15   (—)              v0.5 connector rename + discriminated-union group schema
```

### Design vs implementation

```
Category                              Count   Notes
────────────────────────────────────  ──────  ──────────────────────────────────────────────
Design-doc commits (docs/*.md)        9       v0.4 architecture (5), v0.5 connector (2), README etc.
Implementation commits (src/, tests/) 28      Core code + tests + scripts + skill changes
Ratio (impl / design)                 3.1×    Healthy — code is keeping pace with design
```

### Tags / releases

```
Latest GitHub release:  v0.1.0 (2026-05-08)
Tags in repo:           v0.1.0, v0.3.0, v0.3.1, v0.4.0
Releases NOT cut:       v0.3.0, v0.3.1, v0.4.0   ← tags exist; GitHub release pages don't
```

**Note:** the per-tag GitHub release pages haven't been created for v0.3.0, v0.3.1, or v0.4.0. Tags landed; release notes didn't. Pre-alpha pace argues this is fine; consider a one-time backfill when v0.5.0 cuts.

---

## 2. Work-package burndown — v0.5

State as of 2026-05-15. ✅ Done · 🟡 Partial/Design · ⏸ Not started · ❌ Removed

```
WP    Title                                              Status   Δ this week
────  ─────────────────────────────────────────────────  ───────  ────────────────────
WP-1  /remember autopush + privacy invariant             ✅       NEW: shipped b39691a
WP-4  Qdrant $HOME literal path bug                      ✅       NEW: shipped e0c9d92
WP-J  cowork → mem-fusion infrastructure rename          ✅       NEW: shipped c6839a2
WP-E  SlackConnector implementation                      🟡       Design landed a40e858, 2f91d8d
WP-F  Slack wire format                                  🟡       Covered in WP-E design
WP-B  Extract Connector interface (HttpConnector refac)  ⏸        no change
WP-C  Scheme→executor registry                           ⏸        no change
WP-D  URL-shaped peer-config schema                      ⏸        Connector terminology landed
WP-2  install.sh primary install path                    ⏸        no change
WP-3  Post-install Claude knowledge handoff              ⏸        no change
WP-G  Connector SDK doc                                  ⏸        no change
WP-H  End-to-end validation                              ⏸        no change
WP-I  Propagating edit primitive                         ⏸        no change
WP-A  Tag v0.4.0 final                                   ❌       Removed pre-period
```

```
Burndown:   3 ✅ / 2 🟡 / 8 ⏸ / 1 ❌    (13 active, 23% complete)
Net delta:  +3 done, +2 design-landed, 0 new starts on ⏸ packages
```

**Critical path** for the next 2 weeks: WP-B → WP-D → WP-C-stub → WP-E. None of these started yet.

---

## 3. Adoption / engagement

### Channel membership

```
Joined this week:   6 people + 1 bot user (Mitch's Claude MCP)
Active posters:     4 (Mitch, Shane Pitts, William Battel, + bot)
Lurkers:            3 (Jake Drost, Matt Peterson, Atif Siddiqi — joined, no posts yet*)

* Atif's contribution lives in commit messages, not channel posts.
```

### External user activity

```
User           Posts   Activity
─────────────  ──────  ───────────────────────────────────────────────────────────────
Shane Pitts    3       Confirmed install 2026-05-14; offered Constellation testing on VPN
William Battel 4       Joined 2026-05-14; ran install; reported cold-start issue → fix shipped
Atif Siddiqi   0 *     Found 2 install bugs via install attempt (hooks + scope); both fixed
Jake Drost     0       No activity in window
Matt Peterson  0       No activity in window
```

### External contributions translated into code

```
Bug found by    Commits triggered
──────────────  ──────────────────────────────────────────────────────────────────
Atif Siddiqi    128a2a7 (hook attribute fix), 92a15bc (smoke-test fix)
William Battel  067f6c0 (cold-start uselessness loop fix — 12 files, +1593/-159)
```

**Signal:** every external user who installed in the period found at least one bug. That's a strong product-validation signal but also tells you the install path is still rough. WP-2 (`install.sh`) addresses this directly.

---

## 4. Quality

```
Metric                            Value     Notes
────────────────────────────────  ────────  ─────────────────────────────────────
Test invariants                   202       Up from 171 prior to this week (+31)
Test files                        12        Across tests/{constellation,hooks,installer,mem_fusion}
Open GitHub issues                0         No Issues yet — bugs flow via Slack
Open GitHub PRs                   0         No PR-style contributions yet — sole-committer model
Closed Issues / PRs               0         (same)
```

**Observation:** the repo doesn't yet use GitHub Issues. All bug reports arrive via Slack and become commits via Mitch. This works at current scale (5 collaborators) but won't scale past ~10. Worth opening Issues + Discussions as adoption grows.

---

## 5. Goal alignment

```
Level                                  Status        Last validated      Days since validation
─────────────────────────────────────  ────────────  ──────────────────  ─────────────────────
1  Single-machine semantic memory      ✅ SHIPPED     2026-05-08          7 d (continuous)
2  Multi-machine personal sync         ✅ PROVEN      2026-05-14          1 d
3  Multi-user team groups              🟡 DESIGNED    not yet exercised   N/A
4  Cross-network team sync (Slack)     🟡 DESIGNED    not yet exercised   N/A
5  Public OSS adoption flywheel        🟢 ACTIVE      2026-05-13          2 d (channel created)
6  v1.0 rebrand (claude-mod)           ⏸ DEFERRED    n/a                 N/A
```

**Net:** levels 1–2 validated this week. Levels 3–4 advance via WP-E (SlackConnector) over the next 1–2 weeks. Level 5 is healthy with active external testing. Level 6 correctly parked.

---

## 6. What shipped (top 5)

1. **v0.4.0** (`fa68623`, 2026-05-14) — group-keyed sharing replaces content classification. The core v0.4 architecture lands.
2. **Cold-start fix** (`067f6c0`, 2026-05-14) — closes the "Claude evaluates empty store as not-useful" deadlock. 12 files, +1593/-159.
3. **v0.5 opens** (`b39691a` … `c6839a2`, 2026-05-15) — `/remember` privacy invariant + fix-scripts pattern. WP-1, WP-4, WP-J all land.
4. **v0.5 SlackConnector design** (`a40e858` → `2f91d8d`, 2026-05-15) — docs/v0.5_SLACK_CONNECTOR.md, channel-as-group model, connector terminology locks.
5. **Hook + scope + smoke-test fixes** (`128a2a7`, `1be857b`, `a86b32f`, 2026-05-14) — three external-bug-driven hardening commits that close the cold-start install path.

---

## 7. Queued for next week

Likely to ship (best-confidence first):

```
WP    What                                                Why now
────  ─────────────────────────────────────────────────   ────────────────────────────────
WP-B  Extract Connector interface from v0.4 HTTP code     Pure refactor; unblocks WP-E
WP-D  URL-shaped peer-config schema migration             Mechanical; design is locked
WP-C  Scheme→executor registry (HTTP, Slack stubbed)      Stub now; SlackConnector fills it
WP-E  SlackConnector minimal viable (push-only)           Years-scale features defer to v0.5.1
WP-2  install.sh primary install path                     Reduces install friction (R5 mitigation)
```

At risk of slipping:
- **WP-I** (propagating edits) — design genuinely open; three candidates, no winner.
- **WP-G** (Connector SDK doc) — doc tasks slip when implementation accelerates.
- **v0.5.0 release tag** — no date set; estimated 2 weeks out once WP-B…WP-E land.

---

## 8. Anomalies / worth noting

- **Sole-committer model.** 42/42 commits this week by Mitch. External users found 3 bugs but none submitted patches. As adoption grows, expect this to shift — opening GitHub Issues now is preemptive.
- **GitHub releases lag tags.** v0.3.0, v0.3.1, v0.4.0 exist as tags but not as published release pages. Low priority; backfill when v0.5.0 cuts.
- **Channel-as-data-source asymmetry.** All bug reports flow through Slack. Useful for low-friction adoption; risky for searchability if the project grows. Worth a periodic Slack-to-Issues digest.
- **Connector terminology shift.** Mid-week pivot from "driver" → "connector" (commit `2f91d8d`). Documented in CHANGELOG. Project Update v1 retroactively reconciled. No further drift expected.

---

*Next dashboard: 2026-05-22 (Friday) covering 2026-05-16 → 2026-05-22.*
