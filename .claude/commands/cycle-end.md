# Cycle End

End-of-week cycle wrap-up. Run this at the end of each work cycle.

## Instructions

1. **Fetch the current cycle** from the Quant team in Linear.

2. **Summarize what shipped** — list all issues marked Done this cycle with their titles and PR links. To find PR links: run `gh pr list --state merged` and match by branch name (`noahwinsdev/qnt-XX-*`) for each Done issue.

3. **Identify rollover issues** — any issues still in Todo / In Progress / In Review. For each:
   - Note the current status
   - Check if there's an open branch or PR via `git branch` and `gh pr list`
   - Find the next cycle in Linear (use `list_cycles` — pick the upcoming one by date). If none exists, note it in the report and skip reassignment.
   - Move incomplete issues to the next cycle in Linear and ensure their status is **Todo** (not Backlog) — Backlog issues don't appear on the Linear cycle board

4. **Calculate velocity** — how many issues closed this cycle vs how many were planned.

5. **Check milestone completion** — for each milestone that has issues in this cycle, check if ALL of its issues are now Done (query Linear for the full milestone issue list, not just this cycle's slice). If a milestone is fully complete, prompt: "Phase X is complete — run `/retro Phase X` when ready." Do NOT auto-run retro.

6. **Show a summary report** formatted as:
   ```
   Cycle N (Date — Date)
   ✓ Shipped: X issues
   → Rolled over: Y issues

   Milestones:
     Phase X — Z% complete
     Phase Y — Z% complete  (include all milestones with issues in this cycle)

   Next steps:
     → run /sync-docs to ensure project-plan.md is current
     → run /cycle-start on Monday to kick off the next cycle
   ```
