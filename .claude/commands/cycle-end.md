# Cycle End

End-of-week cycle wrap-up. Run this at the end of each work cycle.

## Instructions

1. **Fetch the current cycle** from the Quant team in Linear.

2. **Summarize what shipped** — list all issues marked Done this cycle with their titles and PR links (if available from git log).

3. **Identify rollover issues** — any issues still in Todo / In Progress / In Review. For each:
   - Note the current status
   - Check if there's an open branch or PR via `git branch` and `gh pr list`
   - Move incomplete issues to the next cycle in Linear

4. **Calculate velocity** — how many issues closed this cycle vs how many were planned.

5. **Update milestone progress** — check if the current milestone's issues are all Done. If so, note that the milestone is complete and suggest starting the next phase.

6. **Show a summary report** formatted as:
   ```
   Cycle N (Date — Date)
   ✓ Shipped: X issues
   → Rolled over: Y issues
   Milestone: Phase X — Z% complete
   ```
