#!/bin/bash
# PostToolUse hook: auto-format Python files after Edit/Write
# Reads tool_input from stdin, runs ruff format on the changed file

INPUT=$(cat)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# Only format Python files
if [ -z "$FILE" ] || [[ "$FILE" != *.py ]]; then
  exit 0
fi

# Only format if the file exists (Write could have failed)
if [ ! -f "$FILE" ]; then
  exit 0
fi

# Run ruff format silently — non-blocking (exit 0 regardless)
uv run ruff format --quiet "$FILE" 2>/dev/null

exit 0
