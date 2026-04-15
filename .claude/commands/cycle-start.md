# Cycle Start

Start-of-week cycle kickoff. Run this at the beginning of each work cycle.

## Instructions

1. **Fetch the current active cycle** from the Quant team in Linear (team ID: `6da338db-71b2-4d14-9519-8a19231e1ccd`). Use `list_cycles` to find the current one.

2. **List all issues in the cycle** by querying Linear for issues assigned to this cycle. Show them in a table:
   - Issue ID (QNT-XX)
   - Title
   - Status (Todo / In Progress / In Review / Done)
   - Priority

3. **Show milestone progress** — which milestone (phase) are we currently working through? How many issues are Done vs remaining?

4. **Suggest the next issue to pick up** based on:
   - Priority (Urgent > High > Medium > Low)
   - Dependencies (blocked issues come after their blockers)
   - Status (skip Done issues)

5. **If the cycle is empty**, suggest pulling issues from the next milestone's backlog into the cycle. When adding issues to the cycle, also move their status from **Backlog → Todo** — Backlog issues don't appear on the Linear cycle board.

6. **Check if `docs/project-plan.md` may be stale** — if there are Done issues in Linear with unchecked items in the plan, note it: "project-plan.md may be out of sync — run `/sync-docs` to reconcile."

7. **Report** formatted as:
   ```
   Cycle N (Date — Date)
   ━━━━━━━━━━━━━━━━━━━━━━━━━

   Issues:
     QNT-XX  Title                  Todo      High
     QNT-YY  Title                  Done      Medium
     ...

   Milestone: Phase X — Name (Z/N done)

   Suggested next: QNT-XX — Title  (Priority)

   ⚠ project-plan.md may be out of sync — run /sync-docs  (if applicable)
   ```

