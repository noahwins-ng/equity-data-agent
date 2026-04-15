# Implement

Implement the code for a Linear issue. Reads the issue, explores the codebase, writes the implementation, and does a quick self-check. Pass the issue identifier as an argument (e.g., `/implement QNT-40`).

The issue identifier is: $ARGUMENTS

## Instructions

### Step 1: Load Context
1. Fetch the issue from Linear — title, description, acceptance criteria, milestone
2. Run `git branch --show-current` to confirm you're on the correct branch
   - If this is a new session and `/session-check` hasn't been run yet, run it now before proceeding
3. Identify the system area from the title/description (Dagster, API, agent, shared, frontend, infra)
4. Read `docs/architecture/system-overview.md` to understand where this fits in the overall system

### Step 2: Explore the Codebase
Before writing any code:
1. Read the relevant package structure (e.g., `packages/dagster-pipelines/src/` for Dagster issues)
2. Look for existing patterns to follow:
   - Similar files already written in the same package
   - How `shared.Settings` is imported and used
   - How other resources/assets are structured
3. Read `packages/shared/src/shared/` — config, schemas, and tickers are referenced by all packages
4. If there are no existing files to follow yet, read `docs/project-requirement.md` for the relevant phase to understand the intended design

### Step 3: Implement
Write the code to satisfy all acceptance criteria:
- Follow existing patterns found in Step 2
- Respect CLAUDE.md rules: LLM never does math, agent never touches DB, three-role architecture
- Use `shared.Settings` for all config — never hardcode hosts, ports, or credentials
- Add type annotations consistent with the rest of the codebase
- Keep it minimal — implement exactly what the AC requires, nothing more

### Step 4: Wire Up
Ensure the new code is importable and connected:
- Add exports to `__init__.py` if needed
- Add the new dependency to `pyproject.toml` if a new package was introduced
- Run `uv sync --all-packages` if `pyproject.toml` was changed

### Step 5: Quick Self-Check
Run the fast checks only (skip tests if they require live infrastructure):
- `uv run ruff check .` — fix any lint errors before reporting
- `uv run ruff format .` — auto-format
- `uv run pyright` — fix type errors before reporting

### Step 6: Report
```
Implemented: QNT-XX — Title
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Files written:
  packages/.../file.py  (new)
  packages/.../other.py  (modified)

Acceptance Criteria:
  ✓ Criterion 1 — implemented in file.py:42
  ✓ Criterion 2 — implemented in file.py:87
  ✓ Criterion 3 — implemented in file.py:23

Checks:
  ✓ Lint    passed
  ✓ Format  passed
  ✓ Types   passed

Ready for /sanity-check QNT-XX
```

If any AC cannot be implemented without manual steps (e.g., requires a live ClickHouse connection to verify), mark it as `? needs manual verification` and explain what to check.
