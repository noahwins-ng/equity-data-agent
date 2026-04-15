# Dev Workflow

How the commands chain together week to week. For command reference (one-liners), see CLAUDE.md.

---

## Weekly Rhythm

```
Monday                    Tuesday–Thursday           Friday
──────────────────────    ──────────────────────    ──────────────────
/cycle-start              /session-check            /cycle-end
  ↓                         ↓ (per session)           ↓
/go QNT-XX                /go QNT-XX (next)         /sync-docs
  ↓                         ↓                       (if plan may be stale)
  shipped ✓                 shipped ✓
```

---

## Issue Lifecycle

**Full auto (recommended):**
```
/go QNT-XX
  → pick → implement → sanity-check → ship
```

**Step by step (when you want control at each stage):**
```
/pick QNT-XX
  → branch checked out, Linear → In Progress

  [new session? run /session-check first to restore context]

/implement QNT-XX
  → explores codebase, writes code to satisfy AC
  → runs lint + format + types as a self-check

/sanity-check QNT-XX
  → lint + format + types + tests + AC check
  → Linear → In Review on pass

/ship QNT-XX
  → if already In Review: skips code checks, re-verifies AC only
  → ticks project-plan.md
  → creates PR, waits for CI, squash merges
  → Linear → Done (auto via "Closes QNT-XX")
  → branch deleted
```

If `/sanity-check` finds failures, fix them and re-run. No separate "rework" command needed.

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
  → prompts: run /sync-docs + run /cycle-start Monday
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
| Linear status out of sync with git/PR state | `/sync-linear QNT-XX` |
| project-plan.md has unchecked Done items | `/sync-docs` |
| Resuming work after a break (on a feature branch) | `/session-check` |
| Unsure what to work on next | `/cycle-start` |
