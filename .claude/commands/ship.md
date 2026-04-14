# Ship

Full shipping pipeline for an issue: sanity check → PR → CI → merge → Linear Done. Pass the issue identifier as an argument (e.g., `/ship QNT-34`).

The issue identifier is: $ARGUMENTS

## Instructions

### Step 1: Sanity Check
Run the full sanity check (same as `/sanity-check`):
- Lint, format, type check, tests
- Verify acceptance criteria from Linear issue

**If any check fails**: stop here, report the failures, and offer to fix them. Do NOT proceed to PR creation.

### Step 2: Commit & Push
- Check for uncommitted changes via `git status`
- If there are changes, stage and commit using the format: `QNT-XX: type(scope): description`
- Push the branch: `git push -u origin HEAD`

### Step 3: Create PR
- Create a pull request using `gh pr create`
- Title: `QNT-XX: <issue title from Linear>`
- Body must include:
  ```
  Closes QNT-XX

  ## Summary
  <bullet points of what changed>

  ## Acceptance Criteria
  <checklist from Linear issue, all checked>

  ---
  Generated with [Claude Code](https://claude.com/claude-code)
  ```

### Step 4: Wait for CI
- Check CI status using `gh pr checks <pr-number> --watch`
- If CI fails: report the failure, do NOT merge, offer to fix

### Step 5: Auto-Merge
- Once CI passes, squash merge: `gh pr merge <pr-number> --squash --delete-branch`
- Switch back to main: `git checkout main && git pull`

### Step 6: Update Project Requirement
- Open `docs/project-requirement.md`
- Find the deliverable(s) that correspond to the shipped issue (match by QNT-XX reference or deliverable description)
- Change `- [ ]` → `- [x]` for each completed deliverable
- Commit: `QNT-XX: docs: mark deliverable complete in project-requirement.md`
- Push to main: `git push`

### Step 7: Update Linear
- Mark the issue as **Done** on Linear
- Add the PR URL as a link attachment on the issue

### Step 8: Report
```
Shipped QNT-XX: Title
━━━━━━━━━━━━━━━━━━━━
PR:     <url> (merged)
Status: Done
Branch: deleted

Milestone: Phase X — Y% complete
Next up:   QNT-YY — <next issue title>
```
