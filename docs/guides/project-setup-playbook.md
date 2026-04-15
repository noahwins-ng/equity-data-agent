# Project Setup Playbook

A reusable checklist for bootstrapping new projects with Claude Code, Linear, and a structured workflow. Derived from the equity-data-agent setup.

---

## Phase 1: Requirements & Planning

### 1.1 Write the project brief
- [ ] Create `docs/project-requirement.md` with:
  - Executive summary and core philosophy
  - Architectural principles (non-negotiable rules)
  - System architecture with Mermaid diagrams
  - Technical stack with rationale
  - Repo structure
  - Data model / schema design
  - Infrastructure & deployment plan
  - Phased project plan with deliverables per phase

### 1.2 Set up Linear
- [ ] Create a **project** under the appropriate team
- [ ] Create **milestones** for each phase (not issues — milestones)
- [ ] Create **issues** for each deliverable within a phase
  - Each issue has: scope, deliverables, acceptance criteria
- [ ] Enable **1-week cycles** on the team (Settings → Team → Cycles)
- [ ] Pull first milestone's issues into Cycle 1 and set their status to **Todo** (Backlog issues don't appear on the cycle board)

---

## Phase 2: Repo Foundation

### 2.1 Initialize
```bash
mkdir project-name && cd project-name
git init && git branch -M main
gh repo create project-name --public --description "..." --source . --push=false
```

### 2.2 Core files to create

| File | Purpose |
|---|---|
| `CLAUDE.md` | AI conventions — auto-loaded every Claude Code session |
| `Makefile` | Dev commands (setup, dev, test, lint, tunnel, issue, pr) |
| `.env.example` | All env vars documented with dev defaults |
| `.gitignore` | Language + framework + IDE + env patterns |
| `.githooks/commit-msg` | Enforces commit message format |

### 2.3 CLAUDE.md template
Include these sections:
1. **Core philosophy** — the non-negotiable rules for this project
2. **Architecture** — how components relate
3. **Stack** — what technologies and why
4. **Repo structure** — where code lives
5. **Code style** — linter, formatter, type checker
6. **Git workflow** — branching, commit format, PR conventions
7. **Working docs** — pointer to `docs/` folder
8. **Observability** — what tools monitor what
9. **Common commands** — Makefile commands + slash commands

### 2.4 Makefile commands to include

> **Stack note**: the quality commands below assume Python (`ruff`, `pyright`, `pytest`). Substitute the equivalent for your stack (e.g. `eslint`/`tsc`/`vitest` for TypeScript, `golangci-lint`/`go test` for Go). The structure and targets should stay the same.

```makefile
# Setup
make setup          # First-time: hooks + deps + .env copy

# Development
make dev-<service>  # One per service (separate terminals)
make tunnel         # If using remote DB via SSH

# Quality (adapt commands to your stack)
make test           # Run tests
make lint           # Linter + type checker
make format         # Auto-format

# Database
make migrate        # Run migrations
make seed           # Quick seed for dev data

# Git workflow (replace TEAM prefix to match your Linear project)
make issue TEAM=XX   # Checkout branch for Linear issue
make pr TEAM=XX TITLE="..." # Push + create PR
```

### 2.5 Git hook (commit-msg)
Enforce your commit format. Example pattern:
```
TEAM-XX: type(scope): description
```
Store in `.githooks/` and configure via `make setup`:
```bash
git config core.hooksPath .githooks
```

---

## Phase 3: Slash Commands

> **Starting point**: copy `.claude/commands/` from `equity-data-agent` — the full 12-command framework is already written and battle-tested. Then do two project-wide replacements:
> 1. Linear team ID (e.g. `6da338db-71b2-4d14-9519-8a19231e1ccd`) → your new team's ID (find it via `list_teams` in Linear MCP)
> 2. Issue prefix `QNT-` → your project's prefix (e.g. `SVC-`, `PLT-`)
>
> Also update the lint/test commands in `sanity-check.md` if your stack differs from Python/uv.

Create `.claude/commands/` with workflow commands:

#### Session & Cycle
| Command | When | What |
|---|---|---|
| `/session-check` | Start of every session | Detect branch → fetch Linear issue → show recent commits + AC status (reads source files, not git log) |
| `/cycle-start` | Start of week | Fetch active cycle, list issues by status/priority, suggest next pick, flag stale plan |
| `/cycle-end` | End of week | Summarize shipped, roll over incomplete issues to next cycle, check milestone completion |
| `/retro [Phase]` | End of milestone | Gather data, capture lessons to memory, update system-overview, sync docs, write retro report |

#### Issue Lifecycle
| Command | When | What |
|---|---|---|
| `/go TEAM-XX` | Work on an issue end-to-end | pick → implement → sanity-check → ship — fully automated, stops only on failure |
| `/pick TEAM-XX` | Starting an issue manually | Checkout branch (uses `gitBranchName` from Linear for full name) → Linear In Progress → display AC |
| `/implement TEAM-XX` | After pick (manual flow) | Explore codebase → write code → lint + format + types self-check |
| `/sanity-check TEAM-XX` | Before shipping (manual flow) | Lint + format + types + tests + AC verification → Linear In Review |
| `/ship TEAM-XX` | Ready to merge (manual flow) | Sanity check (skipped if already In Review) → tick project-plan.md → PR → CI → squash merge → Linear Done |

#### Docs & Scope
| Command | When | What |
|---|---|---|
| `/change-scope add\|drop\|modify` | Requirement changes | Update spec + system-overview + project-plan.md + Linear + ADR if warranted — all in one command |
| `/sync-docs` | Post-change or post-cycle | Tick Done items in project-plan.md, remove Cancelled, surface gaps |
| `/sync-linear TEAM-XX` | Recovery only | Detect state from git/PR, correct Linear status |

### Command design principles
- Each command is self-contained markdown with clear step-by-step instructions
- Commands that take arguments use `$ARGUMENTS` placeholder
- Commands reference the specific Linear team ID and conventions from the project
- `/ship` skips code quality re-checks if the issue is already In Review (avoids redundant work after `/sanity-check`)
- `/change-scope` handles `project-plan.md` updates directly for add/drop/modify — no manual follow-up on the plan
- AC assessment in `/session-check` reads source files, not git log keywords — keyword matching produces false positives
- `/pick` and `/go` must use the `gitBranchName` field from Linear for the full branch name — never create short branches without the description suffix
- When assigning issues to a cycle, always move status Backlog → Todo — Backlog issues don't appear on the Linear cycle board
- Maintain a `docs/guides/dev-workflow.md` as a cadence cheat sheet (not a command reference — that's CLAUDE.md)

---

## Phase 4: Working Docs

Create `docs/` as the project's shared brain:

```
docs/
├── INDEX.md                    # Navigation and purpose
├── project-requirement.md      # Full requirements, architecture, stack
├── project-plan.md             # Phase-by-phase delivery checklists (synced via /ship + /sync-docs)
├── architecture/
│   └── system-overview.md      # How the system works, data flow, component responsibilities
├── decisions/
│   ├── TEMPLATE.md             # ADR template
│   └── 001-first-decision.md   # Why X over Y
├── guides/
│   ├── dev-workflow.md         # Weekly cadence: how commands chain together
│   └── local-dev-setup.md      # Getting started from clone
├── retros/
│   └── phase-0-name.md         # End-of-milestone retrospectives
└── api/
    └── *.http                  # REST Client test files
```

`project-plan.md` is the living delivery checklist — checkboxes per phase, referenced by QNT-XX. It's distinct from `project-requirement.md` (the spec). `/ship` ticks it automatically; `/sync-docs` reconciles it with Linear.

### ADR template
```markdown
# ADR-XXX: Title

**Date**: YYYY-MM-DD
**Status**: Accepted | Superseded | Deprecated

## Context
What prompted this decision?

## Decision
What did we decide, and why?

## Alternatives Considered
What else did we evaluate?

## Consequences
What becomes easier or harder?
```

### When to write an ADR
- Choosing a database, framework, or major library
- Deciding on architecture patterns (monorepo vs multi-repo, sync vs async)
- Making trade-offs that future-you will question ("why didn't we just use X?")

---

## Phase 5: External Setup

### GitHub
- [ ] Create repo (public or private)
- [ ] Set up branch protection on `main`:
  - Require PR (0 approvals for solo dev)
  - Require CI status checks to pass
  - No direct pushes

### CI/CD (GitHub Actions)
- [ ] CI workflow (on PR): lint + type check + tests
- [ ] CD workflow (on push to main): deploy to production

### Observability (pick based on project)
- [ ] **Langfuse** — if using LLM agents (trace thoughts, tools, latency)
- [ ] **Sentry** — API error tracking (free tier: 5k errors/month)
- [ ] **Built-in UIs** — ClickHouse Play, Dagster UI, etc.

---

## Phase 6: Memory & Context

### Claude Code memory
Save to `.claude/projects/<project>/memory/`:

| Type | What to save |
|---|---|
| `user` | Your role, expertise, preferences |
| `feedback` | Workflow corrections and confirmed approaches |
| `project` | Active decisions, constraints, deadlines |
| `reference` | Linear project IDs, external system pointers |

### What NOT to save
- Code patterns (read the code instead)
- Git history (use `git log`)
- Debugging solutions (the fix is in the code)
- Anything already in CLAUDE.md

---

## Checklist Summary

```
[ ] Project brief written (docs/project-requirement.md)
[ ] Project plan written (docs/project-plan.md — phase checklists with QNT-XX references)
[ ] Linear: project + milestones + issues + cycles
[ ] Git repo initialized + GitHub remote
[ ] CLAUDE.md with project conventions
[ ] Makefile with dev commands
[ ] .env.example with all vars
[ ] .gitignore
[ ] .githooks/commit-msg
[ ] .claude/commands/ (12 slash commands)
[ ] docs/ structure (architecture, decisions, guides, retros, api)
[ ] docs/guides/dev-workflow.md (weekly cadence cheat sheet)
[ ] GitHub branch protection enabled
[ ] CI/CD workflows (at least CI)
[ ] Memory files saved for the project
[ ] First ADRs written for key decisions
```

Once all boxes are checked, run `/cycle-start` and begin building.
