# Retrospective

End-of-milestone retrospective. Reviews what happened across all cycles in the current or specified milestone, captures lessons, and preps for the next phase.

Optional argument: milestone name (e.g., `/retro Phase 0: Foundation`). If omitted, uses the most recently completed milestone.

The argument is: $ARGUMENTS

## Instructions

### Step 1: Gather Data
1. **Identify the milestone** — from the argument or find the most recently completed one in Linear
2. **List all issues** in the milestone with: title, status, how many cycles it took, PR link. To find PR links: `gh pr list --state merged --search "QNT-XX"` for each issue.
3. **Check git history** — for each issue in the milestone, run `git log --oneline --grep="QNT-XX"` to find its commits; note any that had multiple commits suggesting rework
4. **Note the timeline** — when did the first issue start? When did the last one close?

### Step 2: Analyze
1. **What shipped** — count of issues closed, list of features/deliverables
2. **Velocity** — issues per cycle, any that took longer than expected
3. **Surprises** — anything that was harder or easier than expected, based on:
   - Issues that were reopened or had multiple PRs
   - Issues that were descoped or split
   - Commits that suggest debugging or rework
4. **Blockers** — anything that stalled progress (external APIs, tooling issues, etc.)

### Step 3: Invariant & Guard Audit
For every incident, outage, or surprise surfaced this period, ask: *"what invariant did this violate, and is it now enforced by CI or a `/ship` hard gate?"*

1. **List the incidents** — any Ops & Reliability ticket closed in this window (e.g., QNT-125, QNT-127, QNT-124) plus any surprise from Step 2. Incidents outside the retro's milestone count if they surfaced during it — Ops & Reliability is perpetual and catches cross-cutting drift.
2. **For each, write one line**:
   ```
   QNT-XX: <one-sentence invariant> — guard: <file path | "NONE — propose QNT-YY">
   ```
   An *invariant* is a one-sentence claim you assumed was true that turned out to drift (e.g., "env_vars in dagster.yaml ⊆ `Settings.model_fields` ∪ allowlist", "no `test_*.py` under `packages/*/tests/**`", "prod SHA == merged commit").
3. **If no guard exists**, the invariant is still on vibes. Either:
   - Draft a new Ops & Reliability ticket now (project "Equity Data Agent", status Todo, label `infra`), **or**
   - Explicitly mark "accepted risk — <one-line reason>".

   Never leave an invariant without a disposition.
4. **Same-shape clustering** — if two incidents violated invariants of the same *shape* (e.g., "file on disk vs. running process" covers both QNT-124 and the Apr 16 SHA drift), flag it. One deeper trap may replace two narrow ones, or reveal a missing cross-cutting guard.

Output carries into Step 10's report under "Invariant guards".

### Step 4: Capture Lessons
For each non-obvious lesson learned:
- Save it to memory (feedback or project type, whichever fits best) so future sessions benefit from it
- Focus on: what to repeat, what to avoid, what surprised us

### Step 5: Prep Next Phase
1. Show the next milestone and its issues
2. Flag any issues that might be risky or underspecified based on lessons learned
3. Suggest which issues to pull into the next cycle — order by priority (Urgent > High > Medium > Low), capped at the average velocity from this milestone (issues closed per cycle)

### Step 6: Phase Review
Feed retro insights forward into upcoming phases. Each completed phase teaches us things that may invalidate, refine, or expose gaps in future plans.

1. **Read upcoming phases** — read `docs/project-requirement.md` and `docs/project-plan.md` for all phases after the one just completed
2. **Cross-reference against lessons** — for each upcoming phase, assess:
   - **Invalidated requirements**: anything now unnecessary given what we've built or learned?
   - **Underspecified requirements**: anything that looked clear on paper but now needs detail given real system knowledge?
   - **Discovered dependencies**: does the build order still make sense, or did we uncover ordering constraints?
   - **Missing requirements**: gaps — things we've learned we need that aren't planned anywhere?
   - **Complexity mismatches**: anything scoped as a single issue that should be split, or multiple issues that are really one?
3. **Produce recommendations** — for each finding, draft a concrete change recommendation:
   ```
   Phase X — <phase title>
     [add | drop | modify] QNT-XX: <one-line summary>
     Reason: <tied to a specific lesson or finding from this retro>
   ```
   Only recommend changes with clear rationale grounded in retro findings — not speculative improvements.
4. **Pause for approval** — present the full list to the user. Do NOT proceed until the user confirms which changes (if any) to action. The user may approve all, approve some, or approve none.
5. **Execute approved changes** — for each approved recommendation, invoke `/change-scope` via the Skill tool with the appropriate arguments (e.g., `/change-scope modify QNT-52 — <reason>`). Run them sequentially — each one updates docs and Linear.

If no changes are warranted, say so and move on. Not every retro will produce scope changes — that's fine.

### Step 7: Update System Overview
Review `docs/architecture/system-overview.md` against what was actually shipped in this milestone:
- New DB tables, columns, or changed schemas → update Databases section
- New or changed API endpoints → update API Endpoint Categories section
- New packages or changed component responsibilities → update the layers table and Package Dependencies
- Infrastructure changes → update Infrastructure section

Update any sections that no longer reflect reality. If nothing changed, skip.

### Step 8: Cleanup
Invoke `/sync-docs` via the Skill tool to reconcile `docs/project-plan.md` with Linear. Do NOT re-implement its logic here.

### Step 9: Post Linear Project Status Update
Post a status update on the Equity Data Agent project using `save_status_update`:
- `type`: `project`
- `project`: `Equity Data Agent`
- `health`: `onTrack` if milestone shipped mostly as planned, `atRisk` if >30% of issues rolled over or were descoped
- `body`: markdown summary:
  ```
  ## Phase X — Complete

  **Shipped:** X issues across Y cycles

  **What went well:**
  - ...

  **Key lessons:**
  - ...

  **Up next:** Phase Y — <brief description>
  ```

### Step 10: Report
Format as:

```
Retrospective: Phase X — Title
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Timeline: <start> → <end> (N cycles)
Shipped:  X issues, Y PRs merged

What went well:
  - ...
  - ...

What was harder than expected:
  - ...

Lessons saved to memory:
  - ...

Invariant guards:
  - QNT-XX: <invariant> → <file path> (added this period)
  - QNT-YY: <invariant> → NONE — proposed QNT-ZZ
  - ...

Phase review:
  - <change-scope actions taken, or "no changes warranted">

Next up: Phase Y — Title (Z issues)
```

### Step 11: Save Retro Report
Write the final report (formatted per Step 10) to `docs/retros/phase-X-name.md` (e.g., `docs/retros/phase-1-data-ingestion.md`). Commit: `docs: add retro for Phase X — Name`.
