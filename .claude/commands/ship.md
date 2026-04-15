# Ship

Full shipping pipeline for an issue: sanity check → PR → CI → merge → Linear Done. Pass the issue identifier as an argument (e.g., `/ship QNT-34`).

The issue identifier is: $ARGUMENTS

## Instructions

### Step 1: Sanity Check
Fetch the current Linear status of the issue first.

**If the issue is already In Review** (meaning `/sanity-check` was run and passed recently):
- Skip lint, format, type check, and tests
- Re-verify acceptance criteria from Linear only (a quick read of relevant files)
- Confirm: "Issue already In Review — skipping code quality checks, re-verifying AC only."

**Otherwise**, run the full sanity check (same as `/sanity-check`):
- Lint, format, type check, tests
- Verify acceptance criteria from Linear issue
- Move Linear issue → **In Review** on pass

**If any check fails**: stop here, report the failures, and offer to fix them. Do NOT proceed to PR creation.

### Step 2: Update Project Plan
- Open `docs/project-plan.md`
- Find the deliverable(s) that correspond to the shipped issue (match by QNT-XX reference or deliverable description)
- Change `- [ ]` → `- [x]` for each completed deliverable
- Stage the file: it will be included in the next commit
- If no matching entry is found, note it in the Step 8 report as "Not in plan — run `/sync-docs` to surface"

### Step 3: Commit & Push
- Check for uncommitted changes via `git status`
- If the working tree is clean (nothing to commit), skip the commit and proceed to push
- Otherwise stage and commit everything (code + doc update) using the format: `QNT-XX: type(scope): description`
- Push the branch: `git push -u origin HEAD`

### Step 4: Create PR
- First check if a PR already exists: `gh pr list --head <branch> --state open`
  - If one exists, use that PR number and skip creation
- Otherwise create a pull request using `gh pr create`
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
- Add the PR URL as a link attachment on the Linear issue (do this now, while the issue is still open)

### Step 5: Wait for CI
- Check CI status using `gh pr checks <pr-number> --watch`
- If no CI checks are present, proceed directly to Step 6
- If CI fails: report the failure, do NOT merge, offer to fix

### Step 6: Auto-Merge
- Once CI passes, squash merge: `gh pr merge <pr-number> --squash --delete-branch`
- Switch back to main: `git checkout main && git pull`

### Step 7: Post-Deploy Verification
After merge, CD runs automatically. Verify the deployed system before marking Done.

**Always run:**
```
make check-prod
```
This SSHs to Hetzner, checks `docker compose ps`, and hits `/health`. If it fails: report the failure and do NOT mark Done.

**For each `⏳ PENDING` prod execution AC item** identified in the sanity check, run the appropriate verification:

| AC type | How to verify |
|---|---|
| Dagster asset registered in prod | `make check-prod` shows dagster service up; or SSH → check prod Dagster API |
| Data in ClickHouse (if not tunnel-verified) | SSH → `docker exec clickhouse clickhouse-client --query "SELECT count() FROM equity_raw.ohlcv_raw"` |
| API endpoint responds in prod | `curl http://<prod-host>:8000/<endpoint>` |
| Prod service healthy | `make check-prod` (health endpoint) |

If all prod execution AC pass: move Linear → **Done** (manual API call — do not rely on GitHub auto-close).
If any prod execution AC fail: keep Linear → **In Review**, report what failed and how to fix it.

### Step 8: Report
Query the active cycle for the highest-priority open issue (not Done) to populate "Next up". If no active cycle exists, omit the "Next up" line.

```
Shipped QNT-XX: Title
━━━━━━━━━━━━━━━━━━━━
PR:     <url> (merged)
Status: Done
Branch: deleted

Milestone: Phase X — Y% complete
Next up:   QNT-YY — <next issue title>  (highest-priority open issue in active cycle)
```
