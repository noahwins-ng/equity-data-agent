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

6. **Run `/sync-docs`** — reconcile `docs/project-plan.md` with Linear: tick Done issues, remove Cancelled issues, surface any gaps.

7. **Post a Linear project status update** on the Equity Data Agent project using `save_status_update`:
   - `type`: `project`
   - `project`: `Equity Data Agent`
   - `health`: `onTrack` if velocity ≥ 50% of planned, `atRisk` if 25–49%, `offTrack` if < 25%
   - `body`: markdown summary with shipped issues (linked), rollover count, velocity, and milestone progress

   Example body:
   ```
   ## Cycle N wrap-up

   **Shipped (X issues):**
   - QNT-XX: Title
   - QNT-YY: Title

   **Rolled over (Y issues):** QNT-ZZ, ...

   **Velocity:** X/N planned issues closed

   **Milestone:** Phase X — Z% complete
   ```

8. **Show a summary report** formatted as:
   ```
   Cycle N (Date — Date)
   ✓ Shipped: X issues
   → Rolled over: Y issues

   Milestones:
     Phase X — Z% complete
     Phase Y — Z% complete  (include all milestones with issues in this cycle)

   Next steps:
     → run /cycle-start on Monday to kick off the next cycle
   ```
