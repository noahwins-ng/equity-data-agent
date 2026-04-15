# Go

Full end-to-end orchestrator for a single issue: pick → implement → sanity-check → ship. Handles micro-commits, AC validation, targeted tests, and error recovery. Pass the issue identifier as an argument (e.g., `/go QNT-40`).

The issue identifier is: $ARGUMENTS

## Instructions

Run each step in sequence. **On failure: diagnose, fix, and retry the failed step — do not stop unless you are truly stuck.** Only ask the user after two failed attempts at the same step.

---

### Step 1: Pick

Run the full `/pick` logic:
- Fetch the issue from Linear (title, description, AC, milestone, relations)
- If any blocking issues are not Done, stop and warn: "Blocked by QNT-XX — resolve before proceeding."
- Checkout the branch using `gitBranchName` from Linear (`git checkout <branch>` or `git checkout -b <branch>` if new)
- Move Linear → **In Progress**
- Show the AC so the user knows what will be built

**Capture the acceptance criteria list — you will reference it throughout all subsequent steps.**

---

### Step 2: Implement (with inline AC tracking and micro-commits)

Run the full `/implement` logic with these enhancements:

#### 2a: Load Context
1. Fetch the issue from Linear — title, description, acceptance criteria, milestone
2. Confirm you're on the correct branch
3. Read `docs/architecture/system-overview.md`
4. Read `docs/patterns.md` — follow established patterns instead of re-discovering them each time
5. Identify the target package and read its structure

#### 2b: Explore Patterns
Before writing code:
1. Read relevant existing files in the target package
2. Check `docs/patterns.md` for a matching recipe (e.g., "Adding a Dagster asset", "Adding an API endpoint")
3. If a pattern exists, follow it exactly. If not, explore 1-2 similar files and follow their structure.

#### 2c: Implement with AC Checkpoints
For each acceptance criterion:
1. Write the code that satisfies it
2. Run `uv run ruff check` and `uv run ruff format` on the changed files (not the whole repo — just the files you touched)
3. Run `uv run pyright` on the changed files
4. **Checkpoint**: After satisfying each AC (or a logical group of ACs), create a WIP commit:
   ```
   QNT-XX: wip: <what was just implemented>
   ```
   This protects against context loss and makes recovery easier.

#### 2d: Targeted Tests
After all AC code is written:
1. Identify which package was changed (e.g., `packages/dagster-pipelines`)
2. Run tests scoped to that package: `uv run pytest packages/<package>/tests/ -x -q` (if tests directory exists)
3. If no tests exist for this package, skip with a note
4. If tests fail: read the error, fix the code, re-run. Do NOT defer to sanity-check.

#### 2e: AC Self-Assessment
Before moving on, evaluate each acceptance criterion:
- Read the relevant code you wrote
- Mark each AC as: DONE / PARTIAL / NOT STARTED
- If any are PARTIAL or NOT STARTED, go back and finish them before proceeding
- This is a self-check — deep verification is `/sanity-check`'s job

---

### Step 3: Sanity Check

Run the full `/sanity-check` logic:
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`
- Verify all AC from Linear
- **On failure**: Do NOT stop. Read the error, fix the code, and re-run the failing check. Only stop after 2 failed fix attempts for the same issue.
- On pass: move Linear → **In Review**

---

### Step 4: Ship

Run the full `/ship` logic:
- Issue is already In Review — skip code quality re-checks, re-verify AC only
- Squash all WIP commits into a clean commit: `QNT-XX: type(scope): description`
  - Use `git reset --soft $(git merge-base HEAD main)` then `git commit` to squash WIPs
- Update `docs/project-plan.md` — tick the completed deliverable
- Push the branch: `git push -u origin HEAD`
- Create PR (or use existing) with body including `Closes QNT-XX`
- Wait for CI
- Squash merge + delete branch
- Linear → Done (auto via "Closes QNT-XX")

---

### Step 5: Report

```
Done: QNT-XX — Title
━━━━━━━━━━━━━━━━━━━━
PR:     <url> (merged)
Status: Done
Branch: deleted

Acceptance Criteria:
  ✓ Criterion 1
  ✓ Criterion 2
  ✓ Criterion 3

Pipeline: pick ✓ → implement ✓ → sanity-check ✓ → ship ✓

Milestone: Phase X — Y% complete
Next up:   QNT-YY — <title>  (run /go QNT-YY to continue)
```

---

## Error Recovery

If any step fails and cannot be auto-fixed after 2 attempts:

1. **Commit a WIP checkpoint** of any progress made so far
2. **Report what failed** with the specific error
3. **Suggest `/fix`** — e.g., "Run `/fix QNT-XX` after resolving the issue to resume the pipeline"
4. **Note which step to resume from** — so `/fix` knows where to pick up
