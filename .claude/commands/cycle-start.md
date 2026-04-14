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

5. **If the cycle is empty**, suggest pulling issues from the next milestone's backlog into the cycle.

6. **Set session context** — state which cycle and milestone we're in so subsequent commands have context.
