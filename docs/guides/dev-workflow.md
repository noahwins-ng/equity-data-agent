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
  → lint + format + types + tests + AC check
  → Linear → In Review on pass

/review QNT-XX
  → adversarial diff review (logic, security, architecture, edge cases)
  → auto-fixes blocking issues

/ship QNT-XX
  → squashes WIP commits into clean commit
  → if already In Review: skips code checks, re-verifies AC only
  → ticks project-plan.md
  → creates PR, waits for CI, squash merges
  → Linear → Done (auto via "Closes QNT-XX")
  → branch deleted
```

If `/sanity-check` finds failures, fix them and re-run. Or use `/fix QNT-XX` to auto-diagnose and resume.

---

## Command Invocation Architecture

Commands are either **leaf** (self-contained) or **composite** (invoke other commands via the Skill tool). Composite commands never re-implement sub-command logic inline — they invoke the actual command so its full instructions load fresh.

### Leaf Commands (depth 0)

These never invoke another command:

`/status`, `/session-check`, `/pick`, `/implement`, `/sanity-check`, `/review`, `/sync-linear`, `/sync-docs`, `/change-scope`, `/cycle-start`

### Composite Commands

| Command | Invokes | Max Depth | Notes |
|---------|---------|-----------|-------|
| `/ship` | `/sanity-check` | 1 | Conditional — skipped if issue already In Review |
| `/fix` | `/sanity-check`, `/review`, `/ship` | 2 | Subset depends on which step failed; `/ship` may invoke `/sanity-check` |
| `/retro` | `/sync-docs` | 1 | Always, in cleanup step |
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
  → lessons saved to memory
  → system-overview.md updated
  → /sync-docs runs as part of retro
  → retro written to docs/retros/phase-X-name.md
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

### Incident Response

| Situation | Action |
|-----------|--------|
| Session-start warns about prod failures | `make monitor-log` → diagnose → `make check-prod` or `make rollback` |
| `/ship` post-deploy health check fails | Retry after 60s → if still failing, `make rollback` |
| Service down but API healthy | `ssh hetzner` → `docker compose --profile prod restart <service>` |
| Need to redeploy without new code | `ssh hetzner 'cd /opt/equity-data-agent && docker compose --profile prod up -d --build'` |

---

## Codebase Patterns

Before implementing, check `docs/patterns.md` — it catalogs established recipes for common tasks:
- Adding a Dagster asset
- Adding a FastAPI endpoint
- Adding an agent tool
- Adding a new ticker
- Adding a ClickHouse migration

Follow the existing pattern. Don't reinvent structure.
