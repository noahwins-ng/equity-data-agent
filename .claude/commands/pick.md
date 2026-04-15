# Pick Issue

Start working on a Linear issue: checkout the branch, mark it In Progress, and surface the acceptance criteria. Pass the issue identifier as an argument (e.g., `/pick QNT-40`).

The issue identifier is: $ARGUMENTS

## Instructions

1. **Fetch the issue** from Linear using the provided identifier.
   - Title, description, acceptance criteria, current status, milestone
   - Include relations (`includeRelations: true`) — if any blocking issues are not Done, warn the user before proceeding: "Blocked by QNT-XX (<title>) — status: <status>. Proceed anyway?"

2. **Checkout the branch**
   - The full branch name is in the `gitBranchName` field from the Linear issue (e.g., `noahwinsdev/qnt-40-dagster-resource-clickhouse-client`)
   - Run `git checkout <gitBranchName>` if the branch already exists, otherwise `git checkout -b <gitBranchName>`
   - Do NOT use `make issue` — it creates a short branch without the description suffix

3. **Move Linear → In Progress**
   - If the issue is already **In Review** or **Done**, warn before proceeding: "QNT-XX is already <status> — move back to In Progress?"
   - Otherwise update the status to **In Progress** regardless of current state (Backlog, Todo, or any pre-active status)
   - Assign the issue to the current active cycle in Linear (fetch via `list_cycles` if needed)

4. **Report** formatted as:
   ```
   Picked up: QNT-XX — Title
   ━━━━━━━━━━━━━━━━━━━━━━━━━
   Milestone: Phase X — Name
   Branch:    noahwinsdev/qnt-XX-description
   Status:    {previous status} → In Progress

   Acceptance Criteria:
     ○ Criterion 1
     ○ Criterion 2
     ○ Criterion 3

   Ready to code. Run /session-check at the start of your next session to restore context.
   ```
