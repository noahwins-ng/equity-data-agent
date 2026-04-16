# Implement

Implement the code for a Linear issue. Reads the issue, explores patterns, writes the implementation, validates AC inline, runs targeted tests, and creates WIP checkpoints. Pass the issue identifier as an argument (e.g., `/implement QNT-40`).

The issue identifier is: $ARGUMENTS

## Instructions

### Step 1: Load Context
1. Fetch the issue from Linear — title, description, acceptance criteria, milestone
2. Run `git branch --show-current` to confirm you're on the correct branch
   - If on `main`, stop: "You're on main — run `/pick QNT-XX` first to checkout the feature branch."
3. Identify the system area from the title/description (Dagster, API, agent, shared, frontend, infra)
4. Read `docs/architecture/system-overview.md` to understand where this fits
5. Read `docs/patterns.md` — check for a matching recipe before exploring the codebase

### Step 2: Explore Patterns
Before writing any code:
1. Check `docs/patterns.md` for a recipe matching your system area (e.g., "Adding a Dagster asset", "Adding an API endpoint")
2. **If a pattern exists**: follow it step-by-step. Read the referenced example files.
3. **If no pattern exists**: fall back to exploration:
   - Read the relevant package structure (e.g., `packages/dagster-pipelines/src/` for Dagster issues)
   - Find 1-2 similar files already written in the same package
   - Read `packages/shared/src/shared/` — config, schemas, tickers
   - If no existing files to follow, read `docs/project-requirement.md` for the relevant phase

### Step 3: Implement with AC Checkpoints
For each acceptance criterion (or logical group):
1. **Write the code** that satisfies the criterion
   - Follow existing patterns from Step 2
   - Respect CLAUDE.md rules: LLM never does math, agent never touches DB, three-role architecture
   - Use `shared.Settings` for all config — never hardcode hosts, ports, or credentials
   - Keep it minimal — implement exactly what the AC requires, nothing more
2. **Quick lint** the changed files (not the whole repo — save pyright for the project-level check in Step 7):
   - `uv run ruff check <file>` — fix any issues
   - `uv run ruff format <file>` — auto-format (the PostToolUse hook handles this for edits, but run it for new files)
3. **WIP commit** after each meaningful chunk (must satisfy `.githooks/commit-msg`):
   ```
   QNT-XX: <type>(<scope>): wip - <brief description>
   ```
   where `<type>` ∈ {feat, fix, refactor, test, docs, chore} and `<scope>` names the package or area. Example: `QNT-90: chore(workflow): wip - add hard gates to /ship`. Plain `QNT-XX: wip:` is rejected by the hook.

   WIP commits protect against session crashes and make `/fix` recovery possible. They are squashed into one clean commit at `/ship`.

### Step 4: Wire Up
Ensure the new code is importable and connected:
- Add exports to `__init__.py` if needed
- Add new dependencies to `pyproject.toml` if a new package was introduced
- Run `uv sync --all-packages` if `pyproject.toml` was changed

### Step 5: Targeted Tests
Run tests scoped to the changed package:
1. Identify the package: `packages/<name>/tests/`
2. If tests exist: `uv run pytest packages/<name>/tests/ -x -q`
3. If tests fail: **read the error, fix the code, re-run** — do NOT defer to sanity-check
4. If no test directory exists for this package, note it and skip

### Step 6: AC Self-Assessment
Before reporting, evaluate each acceptance criterion against the code:
- Read the relevant files you wrote/modified
- For each AC, mark: DONE / PARTIAL / BLOCKED
- **If any are PARTIAL**: go back to Step 3 and finish them
- **If any are BLOCKED**: you MUST name the exact command the user (or Claude) needs to run to unblock it — e.g. `uv run pytest packages/foo/tests/test_x.py::test_y`, `make tunnel && <specific query>`, `ssh hetzner "<specific command>"`. If you cannot name a specific command, the AC is ambiguous — stop and ask the user to clarify rather than marking it manual.
- "Needs manual verification" is NOT an acceptable state. That language has been used in past sessions (QNT-41, QNT-42) as a loophole to ship work before it was actually verified. Either you know the exact verification command (→ BLOCKED with command) or the AC needs the user (→ stop and ask).
- Only proceed when all ACs are DONE or BLOCKED-with-a-specific-command.

### Step 7: Type Check
Run `uv run pyright` on the project. Fix any type errors before reporting.

### Step 8: Report
```
Implemented: QNT-XX — Title
━━━━━━━━━━━━━━━━━━━━━━━━━━━

Files written:
  packages/.../file.py  (new)
  packages/.../other.py  (modified)

Acceptance Criteria:
  ✓ Criterion 1 — implemented in file.py:42
  ✓ Criterion 2 — implemented in file.py:87
  ✗ Criterion 3 — BLOCKED: run `<exact command>` to verify

Checks:
  ✓ Lint      passed
  ✓ Format    passed
  ✓ Types     passed
  ✓ Tests     passed (X tests) | skipped (no test dir)

WIP commits: 3 (will be squashed at /ship)

Ready for /sanity-check QNT-XX
```

If an AC requires an action Claude cannot take (user decision, external credentials, live-system verification), mark it `✗ BLOCKED` with the EXACT command the user should run — e.g. `✗ BLOCKED: run \`make tunnel && docker exec ... clickhouse-client --query '...'\``. "Needs manual verification" without a specific command is not acceptable; if you cannot name a command, the AC is ambiguous and you should stop and ask the user to clarify.
