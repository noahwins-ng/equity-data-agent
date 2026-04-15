#!/bin/bash
# Stop hook: warn about uncommitted work before session ends
# Does NOT block — just injects a warning into Claude's context

BRANCH=$(git branch --show-current 2>/dev/null)

if [ -z "$BRANCH" ] || [ "$BRANCH" = "main" ]; then
  exit 0
fi

UNCOMMITTED=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')

if [ "$UNCOMMITTED" -gt 0 ]; then
  MODIFIED=$(git diff --stat 2>/dev/null | tail -1)
  UNTRACKED=$(git ls-files --others --exclude-standard 2>/dev/null | wc -l | tr -d ' ')

  WARNING="WARNING: You have $UNCOMMITTED uncommitted files on branch $BRANCH ($MODIFIED, $UNTRACKED untracked). Consider committing a WIP checkpoint before ending the session."

  jq -n --arg warning "$WARNING" '{
    additionalContext: $warning
  }'
fi

exit 0
