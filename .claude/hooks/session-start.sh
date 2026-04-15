#!/bin/bash
# SessionStart hook: inject branch context so Claude auto-orients
# Outputs additionalContext JSON that Claude receives at session start

BRANCH=$(git branch --show-current 2>/dev/null)

if [ -z "$BRANCH" ] || [ "$BRANCH" = "main" ]; then
  # On main or not in a git repo — nudge toward cycle-start
  jq -n '{
    additionalContext: "You are on the main branch. No active issue. Suggest: run /cycle-start to review the cycle or /pick QNT-XX to start an issue."
  }'
  exit 0
fi

# Extract QNT issue number from branch name (e.g., noahwinsdev/qnt-41-dagster-asset → QNT-41)
QNT=$(echo "$BRANCH" | grep -oE 'qnt-[0-9]+' | head -1 | sed 's/qnt-//')

if [ -z "$QNT" ]; then
  exit 0
fi

# Gather quick context
UNCOMMITTED=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
COMMITS=$(git log --oneline main...HEAD 2>/dev/null | head -5)
LAST_COMMIT=$(git log --oneline -1 2>/dev/null)

CONTEXT="Resuming session on branch: $BRANCH (QNT-$QNT).
Uncommitted files: $UNCOMMITTED
Recent commits on branch:
$COMMITS

Run /session-check for full context restoration, or /go QNT-$QNT to continue the pipeline."

jq -n --arg ctx "$CONTEXT" '{
  additionalContext: $ctx
}'
exit 0
