# Sync Linear

Manual override to sync an issue's status with Linear. Use this when status has drifted out of sync — normal transitions are handled automatically by `/pick` (→ In Progress), `/sanity-check` (→ In Review), and `/ship` (→ Done). Pass the issue identifier as an argument (e.g., `/sync-linear QNT-34`).

The issue identifier is: $ARGUMENTS

## Instructions

1. **Fetch the issue** from Linear using the provided identifier.

2. **Determine the correct status** based on current state:
   - If there's an active branch with uncommitted work → **In Progress**
   - If there's an open PR → **In Review**
   - If the PR is merged → **Done**
   - If no branch exists yet → **Todo**
   - If the branch was deleted and no PR was ever merged → **Cancelled** (confirm with user before setting)

   Check with: `git branch --list '*qnt-XX*'`, `gh pr list --head <branch>`, `gh pr list --state merged`.

3. **Update Linear** — set the issue status to the determined state.

4. **Report** what changed:
   ```
   QNT-XX: Title
   Status: Todo → In Progress
   Branch: noahwinsdev/qnt-XX-description
   PR: none
   ```

5. If the issue is now **Done**, check if all issues in its milestone are also Done. If so, note that the milestone is complete.
