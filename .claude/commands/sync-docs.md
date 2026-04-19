# Sync Docs

Reconcile `docs/project-plan.md` with Linear issue statuses. Handles Done (tick), Cancelled (remove), and surfaces new issues not yet in the plan. Run after scope changes or whenever docs have drifted from Linear.

## Instructions

### Step 1: Fetch Linear State
Query all Quant team issues. Categorise into:
- **Done** — completed issues
- **Cancelled** — dropped/cancelled issues
- **Active** — Todo / In Progress / In Review (not yet in plan or still open)

### Step 2: Tick Done Items
For each unchecked item in `docs/project-plan.md` that references a Done issue:
- Change `- [ ]` → `- [x]`

### Step 3: Handle Cancelled Items
For each item in `docs/project-plan.md` that references a Cancelled issue:
- Remove it from the plan
- Note it in the report as dropped

Then assess: **does this cancellation warrant an ADR?**
An ADR is warranted if the drop represents a significant architectural or product decision — not just a task being deprioritised. Ask yourself:
- Does it change the system's architecture or data flow?
- Would a future developer wonder "why isn't X here?"
- Does it affect multiple components or downstream phases?

If yes → create a new ADR in `docs/decisions/` using `TEMPLATE.md`. Check `docs/decisions/` for the last numbered file and increment by 1 for the new ADR number. Add it to `docs/INDEX.md` under the decisions section.

### Step 4: Sweep for Issues Not in Plan (mechanical — not by eye)

Build the gap list from two sets, not by scanning. Scan-by-eye is how drift accumulates:

1. **Linear set**: every `QNT-XX` in the Quant-team `Equity Data Agent` project, excluding `Cancelled` and `Duplicate` (those must not be in plan).
2. **Plan set**: `grep -oE 'QNT-[0-9]+' docs/project-plan.md | sort -u`
3. **Gap** = Linear set − Plan set.

For each ID in the gap, report: `QNT-XX: Title (Status, milestone)` under "Not in plan — add manually".

Do NOT auto-add — plan items have sub-bullets and context that can't be generated from a Linear title alone. But DO prompt the user inline for each gap: "Add an entry for QNT-XX under `<milestone>`? (Y/n)". If yes, draft the entry (title + `**Triggered by:**` sub-bullet) and commit with the rest.

Then assess: **does any of these additions warrant an ADR?**
Same criteria as Step 3. If a new issue introduces a new architectural pattern, replaces an existing approach, or affects multiple phases → prompt to create an ADR.

### Step 5: Commit Changes
If not on `main`, warn: "Currently on a feature branch — plan ticks for other issues will be bundled into this PR. Consider checking out main first."

If any items were ticked or removed:
- Commit: `docs: sync project-plan.md with Linear`
- If an ADR was created: `docs: add ADR-XXX — <title>`
- Push to current branch, or directly to main if already on main

### Step 6: Report

```
Synced docs/project-plan.md
━━━━━━━━━━━━━━━━━━━━━━━━━━━

Ticked (Done):
  [x] Item description — QNT-XX

Removed (Cancelled):
  [-] Item description — QNT-XX
      ADR created: docs/decisions/007-... (if applicable)

Not in plan — add manually:
  QNT-XX: Issue title (Status)

ADR prompted:
  QNT-XX: <reason why an ADR is warranted>

Nothing to sync: (if no changes)
```
