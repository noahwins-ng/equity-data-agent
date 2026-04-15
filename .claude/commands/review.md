# Review

Adversarial code review before shipping. Reads the full diff with fresh eyes and looks for logic errors, security issues, missed edge cases, and architectural violations. Run between `/sanity-check` and `/ship`.

Optional argument: issue identifier (e.g., `/review QNT-40`). If omitted, reviews the current branch's diff against main.

The argument is: $ARGUMENTS

## Instructions

### Step 0: Orient

1. If an issue identifier was provided, fetch the issue from Linear for context (title, description, AC)
2. `git branch --show-current` — confirm we're on a feature branch
3. If on `main`, stop: "Nothing to review — you're on main."

### Step 1: Gather the Diff

1. `git diff main...HEAD` — the full diff of everything on this branch
2. `git diff --stat main...HEAD` — summary of files changed
3. `git log --oneline main...HEAD` — commits on this branch
4. If there are uncommitted changes (`git status`), warn: "Uncommitted changes exist — review covers committed state only."

### Step 2: Review (Adversarial)

Read the full diff and review it as if you did NOT write this code. You are a skeptical reviewer seeing it for the first time. Check each category:

#### Logic Errors
- Off-by-one errors, wrong comparisons, inverted conditions
- Missing null/None checks where data could be absent
- Race conditions or ordering assumptions
- Incorrect exception handling (too broad `except`, swallowed errors)

#### Security
- SQL injection (especially in ClickHouse queries — check for f-strings or `.format()` with user input)
- Hardcoded secrets, hosts, or credentials (should use `shared.Settings`)
- Missing input validation on API endpoints (ticker validation, date ranges)
- Path traversal or command injection in any shell calls

#### Architectural Violations
- Does the agent do math? (violates core philosophy)
- Does the agent touch the database directly? (must go through FastAPI)
- Does code cross package boundaries incorrectly? (check `docs/patterns.md` dependency rules)
- Are there hardcoded ticker lists? (must use `TICKERS` from shared)

#### Edge Cases
- What happens with empty data? (no rows returned from ClickHouse, yfinance returns nothing)
- What happens on retry? (idempotency — ReplacingMergeTree handles dupes, but does the code assume single-run?)
- What happens with network failures? (timeouts, connection refused)
- API rate limits (yfinance 429s, LLM API limits)

#### Code Quality (only flag significant issues, not style)
- Dead code or unreachable branches
- Misleading variable names that could cause future bugs
- Missing type annotations on public functions (private functions are fine without)
- Overly complex logic that could be simplified without loss of functionality

### Step 3: Report

```
Review: QNT-XX — Title (or "current branch")
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Files reviewed: N files, +X -Y lines

Issues found:

  🔴 BLOCKING (must fix before ship):
    - [file.py:42] SQL query uses f-string with ticker input — potential injection
    - [file.py:87] Broad except catches SystemExit — use specific exceptions

  🟡 ADVISORY (consider fixing):
    - [file.py:15] No timeout on HTTP request — could hang indefinitely
    - [file.py:63] Empty list case returns None but caller expects list

  ✅ CLEAN (no issues):
    - Logic
    - Architecture
    - Idempotency

Verdict: SHIP / FIX FIRST
```

**SHIP** = no blocking issues found.
**FIX FIRST** = blocking issues exist — list them with file:line and suggested fix.

### Step 4: If FIX FIRST

Offer to fix the blocking issues now. For each:
1. Show the problematic code
2. Show the fix
3. Apply if the user approves (or auto-apply if running inside `/go`)

After fixes, re-read the changed lines to confirm the fix doesn't introduce new issues.

### Step 5: If SHIP

```
Review passed — ready for /ship QNT-XX
```
