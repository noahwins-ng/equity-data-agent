# Ship

Full shipping pipeline for an issue: sanity check → PR → CI → merge → Linear Done. Pass the issue identifier as an argument (e.g., `/ship QNT-34`).

The issue identifier is: $ARGUMENTS

## Instructions

### Step 1: Sanity Check
Fetch the current Linear status of the issue first.

**If the issue is already In Review** (meaning `/sanity-check` was run and passed recently):
- Skip the full sanity check
- Re-verify acceptance criteria from Linear only (a quick read of relevant files)
- Confirm: "Issue already In Review — skipping code quality checks, re-verifying AC only."

**Otherwise**, invoke `/sanity-check` with the issue identifier via the Skill tool. Do NOT re-implement its logic here.
- Wait for it to complete
- If verdict is NEEDS FIXES: stop here, fix the issues, and re-invoke `/sanity-check`
- If verdict is READY TO SHIP: proceed to Step 2

### Step 2: Update Project Plan
- Open `docs/project-plan.md`
- Find the deliverable(s) that correspond to the shipped issue (match by QNT-XX reference or deliverable description)
- Change `- [ ]` → `- [x]` for each completed deliverable
- Stage the file: it will be included in the next commit
- If no matching entry is found, note it in the Step 8 report as "Not in plan — run `/sync-docs` to surface"

### Step 3: Squash WIP Commits, Commit & Push
- Check for uncommitted changes via `git status`
- If there are uncommitted changes, stage them: `git add -A`
- **Squash all WIP commits** into one clean conventional commit:
  ```bash
  git reset --soft $(git merge-base main HEAD)
  git commit -m "QNT-XX: type(scope): description"
  ```
  This preserves all changes but replaces the WIP history with a single commit.
- If there are no WIP commits (only one clean commit already), skip the squash
- **Check if branch is behind main** before pushing:
  ```bash
  git fetch origin main
  git log HEAD..origin/main --oneline
  ```
  If main has new commits: rebase first with `git rebase origin/main`. If conflicts arise, report them and stop — do not auto-resolve.
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
After merge, CD runs automatically. **Wait for deployment to propagate before verifying** — the old containers need time to be replaced.

1. Check CD status first: `ssh hetzner 'docker compose -f /opt/equity-data-agent/docker-compose.yml ps --format json'` to see container uptimes. If containers were created more than 5 minutes ago, CD may not have triggered yet — wait and re-check.
2. If CI/CD is still running, wait ~90 seconds and re-check. Do not run `make check-prod` against a stale deployment.

**Once deployment is fresh, run:**
```
make check-prod
```
This SSHs to Hetzner, checks `docker compose ps`, and hits `/health`. If it fails: retry once after 60 seconds. If it still fails:
- Report the failure and do NOT mark Done
- Suggest rollback: `make rollback` (reverts prod to the previous commit and rebuilds)
- If the user confirms rollback, run it and report the result

**For each `⏳ PENDING` prod execution AC item** identified in the sanity check, run the appropriate verification:

| AC type | How to verify |
|---|---|
| Dagster asset registered in prod | `make check-prod` shows dagster service up; or SSH → check prod Dagster API |
| Data in ClickHouse (if not tunnel-verified) | SSH → `docker exec clickhouse clickhouse-client --query "SELECT count() FROM equity_raw.ohlcv_raw"` |
| API endpoint responds in prod | `curl http://<prod-host>:8000/<endpoint>` |
| Prod service healthy | `make check-prod` (health endpoint) |

If all prod execution AC pass: move Linear → **Done** (manual API call — do not rely on GitHub auto-close).
If any prod execution AC fail: keep Linear → **In Review**, report what failed and how to fix it.

### Step 7b: Post Shipped Comment on Linear Issue

Post a comment on the Linear issue summarizing the ship:
```
**Shipped** — PR #<number> merged, deployed, verified

✓ Lint  ✓ Format  ✓ Types  ✓ Tests  ✓ AC

**Dev execution AC verified:**
- <each dev execution AC with evidence summary>

**Prod execution AC verified:**
- <each prod execution AC result>
```

This creates a permanent audit trail on the issue. Every shipped issue should have at least one comment showing what was verified.

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
