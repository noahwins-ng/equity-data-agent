#!/bin/bash
# PreToolUse hook: block dangerous git commands
# Exit 2 = block the action, stderr becomes feedback to Claude

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ -z "$COMMAND" ]; then
  exit 0
fi

# Block force push
if echo "$COMMAND" | grep -qE 'git\s+push\s+.*--force|git\s+push\s+-f\b'; then
  echo "Blocked: git push --force is not allowed. Use regular push." >&2
  exit 2
fi

# Block force push to main/master
if echo "$COMMAND" | grep -qE 'git\s+push\s+.*\b(main|master)\b'; then
  echo "Blocked: pushing directly to main/master is not allowed. Push to a feature branch." >&2
  exit 2
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
