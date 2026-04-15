# Status

Lightweight context check — quick glance at where you are without fetching from Linear or doing deep analysis. No arguments needed.

## Instructions

Run these commands and report the results. No Linear API calls, no file reading beyond git — this should be instant.

1. **Branch**: `git branch --show-current`
2. **Issue**: extract QNT-XX from branch name (e.g., `noahwinsdev/qnt-41-dagster-asset` → QNT-41)
3. **Uncommitted work**: `git status --short` — count modified, staged, and untracked
4. **Commits on branch**: `git log --oneline main...HEAD` (count + last 3)
5. **Diff size**: `git diff --stat main...HEAD` — total files changed, insertions, deletions

### Report

```
Status
━━━━━━
Branch:      noahwinsdev/qnt-XX-description
Issue:       QNT-XX
Commits:     N on branch
Uncommitted: M modified, S staged, U untracked

Last 3 commits:
  abc1234 QNT-XX: wip: thing one
  def5678 QNT-XX: wip: thing two
  ghi9012 QNT-XX: feat(scope): initial

Diff vs main: X files changed, +Y -Z

Quick actions:
  /go QNT-XX         resume full pipeline
  /session-check     full context restore (reads Linear + AC)
  /sanity-check QNT-XX  run quality gate
```

If on `main`:
```
Status
━━━━━━
Branch: main
No active issue.

Quick actions:
  /cycle-start     review cycle and pick next issue
  /pick QNT-XX     start a specific issue
```
