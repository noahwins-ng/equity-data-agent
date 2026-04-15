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
| **session-start** | Session begins | Detects branch, injects QNT context, suggests next action |
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

---

## Codebase Patterns

Before implementing, check `docs/patterns.md` — it catalogs established recipes for common tasks:
- Adding a Dagster asset
- Adding a FastAPI endpoint
- Adding an agent tool
- Adding a new ticker
- Adding a ClickHouse migration

Follow the existing pattern. Don't reinvent structure.
