# Dev Workflow

How the commands chain together week to week. For command reference (one-liners), see CLAUDE.md.

---

## Weekly Rhythm

```
Monday                    Tuesday–Thursday           Friday
──────────────────────    ──────────────────────    ──────────────────
/cycle-start              /session-check            /cycle-end
  ↓                         ↓ (auto via hook)         (auto-runs /sync-docs,
/go QNT-XX                /go QNT-XX (next)          posts Linear update,
  ↓                         ↓                         rolls over incomplete)
  shipped ✓                 shipped ✓
```

**Note**: The `SessionStart` hook auto-detects your branch and injects context at the start of every session. You still run `/session-check` for full Linear + AC restoration, but the hook gives you immediate orientation.

---

## Issue Lifecycle

**Full auto (recommended):**
```
/go QNT-XX
  → pick
  → implement (with WIP commits + AC checkpoints + targeted tests)
  → sanity-check (with auto-fix on failure)
  → review (adversarial code review — logic, security, edge cases)
  → ship (squash WIPs, PR, CI, merge)
```

`/go` is the true end-to-end orchestrator. It handles errors by diagnosing and fixing them — not by stopping at the first failure. It only asks you after 2 failed attempts at the same step.

**Step by step (when you want control at each stage):**
```
/pick QNT-XX
  → branch checked out, Linear → In Progress

  [new session? SessionStart hook gives you context automatically]
  [want full context? run /session-check]

/implement QNT-XX
  → reads docs/patterns.md for established recipes
  → writes code with WIP commits after each AC
  → runs targeted tests on the changed package
  → validates all AC before reporting

/sanity-check QNT-XX
  → lint + format + types + tests
  → AC check using the three-class taxonomy (QNT-90):
      • [code AC]          — verified by reading the code; mark PASS/FAIL
      • [dev execution AC] — must run the command + paste output as evidence (keywords like
                             "populated", "returns", "visible", "schedule enabled" trigger this)
      • [prod execution AC] — only verifiable post-deploy; mark ⏳ PENDING
      • ⏳ PENDING         — verifier belongs to a later phase (e.g., frontend selector on
                             an API ticket); consumer ticket inherits the AC (Phase 3 lesson)
  → Linear → In Review on pass

/review QNT-XX
  → adversarial diff review (logic, security, architecture, edge cases)
  → re-checks that every dev execution AC has command + output evidence
  → auto-fixes blocking issues

/ship QNT-XX
  → squashes WIP commits into clean commit
  → if already In Review: skips code checks, re-verifies AC only
  → ticks project-plan.md
  → creates PR, waits for CI, squash merges
  → post-deploy hard gates (must both pass before trusting any AC result):
      (a) prod `git rev-parse HEAD` equals merge-commit SHA (QNT-88 — catches silent
          stale-deploy drift, Apr-16 outage pattern)
      (b) Dagster definitions module loads with the expected asset/check/schedule counts
          (QNT-89 — catches "container up but Python didn't actually load" drift)
  → make check-prod (services + /health)
  → verifies each ⏳ PENDING prod-execution AC with the appropriate command
  → Linear → Done via explicit API call (NOT relying on GitHub "Closes QNT-XX" — save_issue
    with links can silently revert state; a manual state write is load-bearing)
  → posts shipped comment on Linear with evidence (audit trail)
  → branch deleted
```

If `/sanity-check` finds failures, fix them and re-run. Or use `/fix QNT-XX` to auto-diagnose and resume.

---

## Command Invocation Architecture

Commands are either **leaf** (self-contained) or **composite** (invoke other commands via the Skill tool). Composite commands never re-implement sub-command logic inline — they invoke the actual command so its full instructions load fresh.

### Leaf Commands (depth 0)

These never invoke another command:

`/status`, `/session-check`, `/pick`, `/implement`, `/sanity-check`, `/review`, `/sync-linear`, `/sync-docs`, `/change-scope`, `/cycle-start`, `/server-audit`

### Composite Commands

| Command | Invokes | Max Depth | Notes |
|---------|---------|-----------|-------|
| `/ship` | `/sanity-check` | 1 | Conditional — skipped if issue already In Review |
| `/fix` | `/sanity-check`, `/review`, `/ship` | 2 | Subset depends on which step failed; `/ship` may invoke `/sanity-check` |
| `/retro` | `/sync-docs`, `/change-scope` | 1 | `/sync-docs` always (cleanup); `/change-scope` conditionally, one invocation per approved forward-looking scope change |
| `/cycle-end` | `/sync-docs` | 1 | Always, in cleanup step |
| `/go` | `/pick`, `/implement`, `/sanity-check`, `/review`, `/ship` | 2 | All 5 in sequence |

### Invocation Chains

```
/go → /pick                                  (depth 1)
/go → /implement                             (depth 1)
/go → /sanity-check                          (depth 1)
/go → /review                                (depth 1)
/go → /ship                                  (depth 1, issue already In Review)
/go → /ship → /sanity-check                  (depth 2, only if In Review was skipped)

/fix → /sanity-check                         (depth 1)
/fix → /review                               (depth 1)
/fix → /ship                                 (depth 1)
/fix → /ship → /sanity-check                 (depth 2, only if issue not In Review)

/retro → /sync-docs                          (depth 1)
/retro → /change-scope (× N, optional)       (depth 1, one per approved scope change)
/cycle-end → /sync-docs                      (depth 1)
```

**Design rule**: Max depth is 2. No command invokes a composite that invokes another composite beyond one level. This keeps context window growth predictable.

---

## Quick Commands

| Command | Purpose | Speed |
|---------|---------|-------|
| `/status` | Where am I? Branch, commits, uncommitted work | Instant (no API calls) |
| `/session-check` | Full context restore (reads Linear + AC) | ~5 seconds |
| `/fix QNT-XX` | Diagnose failure, fix, resume pipeline | Varies |

---

## Hooks (Automatic Behaviors)

Hooks run automatically — you don't invoke them. They're configured in `.claude/settings.json`.

| Hook | Event | What it does |
|------|-------|--------------|
| **session-start** | Session begins | Detects branch, injects QNT context, warns on prod health failures |
| **auto-format** | After Edit/Write | Runs `ruff format` on every Python file Claude edits |
| **protect-repo** | Before Bash | Blocks `git push --force`, `git reset --hard`, `rm -rf .`, push to main |
| **check-uncommitted** | Session ends | Warns about uncommitted work before Claude stops |

Hook scripts live in `.claude/hooks/`. The configuration is in `.claude/settings.json`.

---

## Scope Changes (mid-issue or mid-cycle)

```
/change-scope add|drop|modify <description>
  → updates docs/project-requirement.md
  → updates docs/architecture/system-overview.md (if architecture changed)
  → updates docs/project-plan.md (add entry / remove entry / update text)
  → creates/cancels/updates Linear issue
  → creates ADR if warranted

/sync-docs
  → ticks Done items in project-plan.md
  → removes Cancelled items
  → surfaces issues missing from plan
```

Note: `/change-scope` updates `project-plan.md` text directly for all three change types — no manual follow-up needed. Run `/sync-docs` only to reconcile unrelated Done/Cancelled statuses.

---

## Milestone Close

```
/cycle-end
  → summarizes shipped, rolls over incomplete to next cycle
  → auto-runs /sync-docs (ticks Done items, removes Cancelled)
  → posts Linear project status update
  → if milestone complete: "run /retro Phase X when ready"

/retro [Phase X]
  → git + Linear data gathered automatically
  → lessons saved to memory (workflow rules)
  → system-overview.md reviewed + updated if shipped scope changed it
  → forward-looking scope changes applied via /change-scope (after user approval)
  → /sync-docs runs as part of retro
  → retro written to docs/retros/phase-X-name.md
  → Linear project status update posted (onTrack / atRisk)
  → manual follow-up (not yet automated): promote reusable code recipes to
    docs/patterns.md — see "Codebase Patterns → Where patterns come from" below
```

---

## Recovery

| Situation | Command |
|-----------|---------|
| `/go` pipeline failed mid-way | `/fix QNT-XX` — diagnoses, fixes, resumes |
| Don't know where I am | `/status` — instant branch/commit/uncommitted check |
| Resuming after a break (full context) | `/session-check` (or just start — SessionStart hook gives basics) |
| Linear status out of sync with git/PR | `/sync-linear QNT-XX` |
| project-plan.md has unchecked Done items | `/sync-docs` |
| Unsure what to work on next | `/cycle-start` |
| Prod health failures detected at session start | `make monitor-log` — check details, then `make check-prod` or `make rollback` |

---

## Production Operations

### Health Monitoring

A cron job on Hetzner checks API health + Docker services every 15 minutes.

```bash
make monitor-install    # one-time setup (already done)
make monitor-log        # check heartbeat + recent failures
make check-prod         # manual full health check (services + /health)
```

The **session-start hook** automatically checks for recent prod failures and warns at the top of every session. If you see the warning, run `make monitor-log` for details.

### Rollback

If a deploy breaks prod:

```bash
make rollback           # reverts to previous commit, rebuilds, verifies health
```

This is also suggested automatically by `/ship` if post-deploy verification fails.

### Server Audit

```
/server-audit
  → audits Hetzner prod across four dimensions: durability / host / security / drift
  → surfaces gaps as proposed Linear tickets (Ops & Reliability milestone)
  → files them on approval
```

Run periodically (e.g., monthly) or after any incident. Complements the reactive QNT-88/89/95/96 hardening by sweeping for gaps that haven't triggered an incident yet.

### Incident Response

| Situation | Action |
|-----------|--------|
| Session-start warns about prod failures | `make monitor-log` → diagnose → `make check-prod` or `make rollback` |
| `/ship` post-deploy health check fails | Retry after 60s → if still failing, `make rollback` |
| Service down but API healthy | `ssh hetzner` → `docker compose --profile prod restart <service>` |
| Need to redeploy without new code | `ssh hetzner 'cd /opt/equity-data-agent && docker compose --profile prod up -d --build'` |

---

## Codebase Patterns

Before implementing, check `docs/patterns.md` — it catalogs established recipes for common tasks (Dagster asset, FastAPI endpoint, agent tool, new ticker, ClickHouse migration, export patterns, etc.).

Follow the existing pattern. Don't reinvent structure.

### Where patterns come from

`patterns.md` is maintained manually. Retros are the natural discovery moment — a ticket that invented a reusable recipe in a specific package should have that recipe lifted into `patterns.md` so the next session finds it.

**Memory vs patterns (they're different)**:

| | Memory (`feedback_*.md`) | `patterns.md` |
|---|---|---|
| **Scope** | Workflow rule — "when/why to apply" | Code recipe — literal paste-ready implementation |
| **Discovery** | Auto-loaded every session | Manually read during `/implement` Step 1 |
| **Example** | "When testing external services, mock at the client boundary" | The 15-line `_FakeClient` class + the `monkeypatch.setattr(...)` wiring |

If a retro identifies a reusable code recipe but the recipe never makes it into `patterns.md`, the next `/go` either rediscovers it (waste) or reinvents a worse version (drift). Treat pattern extraction as a retro follow-up, not a nice-to-have.

**Pending extractions** (recipes surfaced but not yet in `patterns.md`):

- Fake external-service client fixture (Phase 3 `_FakeClient` in `packages/api/tests/test_data.py`) — applies to ClickHouse today, Qdrant in Phase 4 (QNT-54/55 scope explicitly requires it)
