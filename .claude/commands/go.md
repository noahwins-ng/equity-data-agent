# Go

Full end-to-end pipeline for a single issue: pick → implement → sanity-check → ship. Pass the issue identifier as an argument (e.g., `/go QNT-40`).

The issue identifier is: $ARGUMENTS

## Instructions

Run each step in sequence. Stop immediately if any step fails — do not proceed to the next step.

### Step 1: Pick
Run the full `/pick` logic:
- Fetch the issue from Linear (title, description, AC, milestone, relations)
- If any blocking issues are not Done, stop and warn: "Blocked by QNT-XX — resolve before proceeding."
- Checkout the branch using `gitBranchName` from Linear (`git checkout <branch>` or `git checkout -b <branch>` if new)
- Move Linear → **In Progress**
- Show the AC so the user knows what will be built

### Step 2: Implement
Run the full `/implement` logic:
- Read `docs/architecture/system-overview.md` and relevant package structure
- Explore existing patterns in the codebase
- Write code to satisfy all AC
- Run `uv run ruff check .`, `uv run ruff format .`, `uv run pyright`
- Fix any lint or type errors before proceeding

### Step 3: Sanity Check
Run the full `/sanity-check` logic:
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`
- Verify all AC from Linear — classify each as **code AC** (verifiable by reading) or **execution AC** (must have actually been run). Any execution AC not yet demonstrated is `✗ BLOCKED` and prevents ship.
- If any check fails or any AC is BLOCKED: stop, report what must be run, do NOT proceed to ship.
- On pass (all AC satisfied including execution ones): move Linear → **In Review**

### Step 4: Ship
Run the full `/ship` logic:
- Issue is already In Review — skip code quality re-checks, re-verify AC only
- Tick `docs/project-plan.md`
- Commit + push
- Create PR (or use existing)
- Wait for CI
- Squash merge + delete branch
- Post-deploy: run `make check-prod`, verify any `⏳ PENDING` prod execution AC items
- Linear → Done only after prod verification passes

### Step 5: Report
```
Done: QNT-XX — Title
━━━━━━━━━━━━━━━━━━━━
PR:     <url> (merged)
Status: Done
Branch: deleted

Milestone: Phase X — Y% complete
Next up:   QNT-YY — <title>  (run /go QNT-YY to continue)
```
