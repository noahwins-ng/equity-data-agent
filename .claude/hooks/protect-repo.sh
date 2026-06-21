#!/bin/bash
# PreToolUse hook: block dangerous git commands
# Exit 2 = block the action, stderr becomes feedback to Claude

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ -z "$COMMAND" ]; then
  exit 0
fi

# Treat the command as a `git push` only when it appears as an actual command
# (start of the string or after a shell separator: ; & | && ||), NOT when the
# text merely mentions one inside a heredoc, echo, or comment. Without this
# boundary the old substring match false-positived on any command whose text
# contained "git push ... main" (e.g. writing this very file).
if echo "$COMMAND" | grep -qE '(^|[;&|])[[:space:]]*git[[:space:]]+push\b'; then

  # Block force push (any target)
  if echo "$COMMAND" | grep -qE 'git[[:space:]]+push[[:space:]].*(--force|--force-with-lease|-f\b)'; then
    echo "Blocked: git push --force is not allowed. Use regular push." >&2
    exit 2
  fi

  # Block an explicit main/master in the refspec (e.g. `git push origin main`),
  # which is dangerous even from a feature branch.
  if echo "$COMMAND" | grep -qE 'git[[:space:]]+push[[:space:]].*\b(main|master)\b'; then
    echo "Blocked: pushing directly to main/master is not allowed. Push to a feature branch." >&2
    exit 2
  fi

  # State-aware guard (the QNT-264 gap): a bare `git push`, `git push origin
  # HEAD`, or `git push -u origin HEAD` names no branch literal but still updates
  # origin/<current-branch>. Resolve the branch HEAD actually points at and block
  # when it is main/master -- the string match above could never catch this.
  CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ "$CURRENT_BRANCH" = "main" ] || [ "$CURRENT_BRANCH" = "master" ]; then
    echo "Blocked: HEAD is on '$CURRENT_BRANCH' -- this push would update origin/$CURRENT_BRANCH. Switch to a feature branch first (git switch -c <branch>)." >&2
    exit 2
  fi
fi

# Block hard reset
if echo "$COMMAND" | grep -qE 'git\s+reset\s+--hard'; then
  echo "Blocked: git reset --hard discards work. Use git stash or git reset --soft instead." >&2
  exit 2
fi

# Block branch deletion of main
if echo "$COMMAND" | grep -qE 'git\s+branch\s+-[dD]\s+(main|master)'; then
  echo "Blocked: cannot delete main/master branch." >&2
  exit 2
fi

# Block checkout that discards changes (git checkout -- .)
if echo "$COMMAND" | grep -qE 'git\s+checkout\s+--\s+\.'; then
  echo "Blocked: git checkout -- . discards all changes. Use git stash instead." >&2
  exit 2
fi

# Block rm -rf on project root or packages
if echo "$COMMAND" | grep -qE 'rm\s+-rf\s+(\.|/|packages|docs|\.claude)'; then
  echo "Blocked: rm -rf on critical directories is not allowed." >&2
  exit 2
fi

exit 0
