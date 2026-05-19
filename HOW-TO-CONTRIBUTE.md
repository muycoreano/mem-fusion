# How to Contribute (for Claude AIs)

This guide is for **another Claude AI** running mem-fusion locally that wants to report a bug, propose an enhancement, or land a code change.

Repo: <https://github.com/muycoreano/mem-fusion>
Trunk: `main` (no v0.5 / v0.6 feature branches — commit there)
Working channel: `#mem-fusion-alpha-test` (Slack ID `C0B4SS5N5JR`) — where everyone starts
Audience: this is a **PUBLIC** OSS project. Never include internal business context (customers, finances, employee names, unrelated initiatives) in any artifact you push here.

---

## 1. Report a bug — store a memory

Use the local `mem-fusion` MCP. The memory is the bug report.

```
store_memory(
  content   = "BUG <short title>. Symptom: <what happened>. Repro: <commands>.
               Expected: <X>. Actual: <Y>. Environment: <node_name, OS, model>.
               Workaround: <if any>.",
  type      = "error",
  project   = "mem-fusion",
  importance= 3,
  tags      = ["bug-report", "<area-tag>"],
  groups    = ["personal"]
)
```

Then push to the working channel so the team sees it:

```
add_connector_ids(memory_ids=[id], connector_ids=["mem-fusion-alpha-test"])
# orchestration sends via Slack MCP to #mem-fusion-alpha-test
```

Or just say the human-form: `/remember <bug content> for mem-fusion-alpha-test`.

**Minimum bug-report fields:** symptom, repro, expected vs actual, environment (node_name + OS + Claude model). Workaround optional.

---

## 2. Propose an enhancement — store a memory

```
store_memory(
  content   = "ENHANCEMENT <short title>. Rationale: <why>. Sketch: <how>.
               Risk: <what breaks>. Alternatives considered: <briefly>.",
  type      = "decision",
  project   = "mem-fusion",
  importance= 4,
  tags      = ["enhancement-proposal", "<area-tag>"],
  groups    = ["personal"]
)
```

Push to the working channel for review. The architect (whoever currently wears that hat) reviews + comments via reply memory or a Slack thread.

---

## 3. Land a code change

### 3a. Tests first

Run the relevant suite before committing:

```bash
~/.local/share/mem-fusion/venv/bin/python tests/mem_fusion/test-<area>.py
```

Tests under `tests/mem_fusion/` are standalone; each exits 0 on pass, 1 on failure. New code must come with new tests in the same shape.

### 3b. For in-place patches to installed peers — fix script

If your change must roll out to peers that have already installed mem-fusion, write a fix script:

```
src/scripts/fixes/<version>-<seq>-<description>.sh
```

E.g., `src/scripts/fixes/0.5.0-020-fix-foo.sh`. Conventions:
- Self-contained, idempotent (re-run is a no-op)
- Detects "already applied" via a deployed-state marker
- Documented in `src/scripts/fixes/README.md`
- Verified live on at least one peer before commit

### 3c. CHANGELOG.md

Every behavior change gets a CHANGELOG entry under `[Unreleased]` — `Added` / `Changed` / `Fixed` / `Deprecated` / `Removed` / `Security` per Keep a Changelog convention. Be specific: name the file, the symptom, the fix, the verification.

### 3d. Commit message + trailer

```
<short subject — what changed, not why (why goes in body)>

<one paragraph: why, what was wrong before, what's right now>

<optional: extra context, follow-up, references>

Co-Authored-By: Claude-<Role> (<Model>, <node_name>) <noreply@anthropic.com>
```

Role tag follows the node_name → role mapping (see `docs/v0.5_CONNECTOR_ARCHITECTURE.md` and any session-start memory for the current convention). When unsure, use plain `Claude` — never invent a role tag.

### 3e. Push + announce

The **post-commit cycle** is canonical (memory `564f3102` / `8db1b4a7`):

1. Implement
2. Update CHANGELOG.md
3. `git add` only the files you touched (NOT `-A` or `.`)
4. `git commit` with the trailer above
5. `git push origin main`
6. **Draft** a Slack announcement for `#mem-fusion-alpha-test`
7. **Wait for explicit human approval** ("post it" / "send it")
8. On approval, `slack_send_message` to `C0B4SS5N5JR`

Never skip step 7. Auto-posting even on direct-sounding requests still requires the draft pass.

### 3f. Slack post format (durable rule, memory `f27049c7`)

- Backtick lede (the title)
- Bold value sentence (the takeaway, one or two lines)
- Fixed-width table for "file / change" content
- `**bold**` not `*bold*` (Slack MCP translates)
- **Never** link local filesystem paths (`~/...` / `/Users/...`) — readers don't have access
- **Always** close with: `Memorized and shared with mem-fusion: <memory-uuid>`
- Sign with your role tag

---

## 4. Direct PRs from external contributors

External humans submit PRs to <https://github.com/muycoreano/mem-fusion> on `main`. Architect (current hat-holder) reviews. Code-level review uses the `/review` skill; security-sensitive changes use `/security-review`.

---

## 5. Things that are NOT contributions

Do not commit:
- Branch (employer) internal business context — customer names, financials, MendRx, Pay Admin, HYSA, EWA, internal political details
- Machine-specific config (paths with usernames, ports, tokens) — only generic paths
- Secrets of any kind (API keys, OAuth tokens, passwords) — even in tests; use env-var pointers
- Hand-edited test data that bypasses production helpers (architect lock — staff audit memory `250e8a54`)
- AI-generated marketing fluff in CHANGELOG entries — be specific and factual

---

## 6. Where the canonical references live

- `docs/v0.5_CONNECTOR_ARCHITECTURE.md` — connector wire format, dispatch contract
- `docs/v0.5_INSTALL_FLOW.md` — install.sh architecture
- `docs/v0.6_PERSONAL_SYNC.md` — current personal-sync design
- `docs/v0.5_STAFF_AUDIT_2026-05-18.md` — recent architectural decisions
- `CHANGELOG.md` — every shipped change since v0.1.0
- `src/scripts/fixes/README.md` — fix-script pattern
- `~/.claude/skills/remember/SKILL.md` — `/remember` semantics + connector routing

---

## 7. When in doubt

Search memory first: `search_memory(query="<topic>", top_k=8)`. Most architectural questions have already been answered; the store grows with every session. If you can't find an answer, store the question (`type="decision"`, importance=4) and push to `#mem-fusion-alpha-test` — someone will pick it up.
