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
- [ ] Pull first milestone's issues into Cycle 1

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

```makefile
# Setup
make setup          # First-time: hooks + deps + .env copy

# Development
make dev-<service>  # One per service (separate terminals)
make tunnel         # If using remote DB via SSH

# Quality
make test           # Run tests
make lint           # Linter + type checker
make format         # Auto-format

# Database
make migrate        # Run migrations
make seed           # Quick seed for dev data

# Git workflow
make issue QNT=XX   # Checkout branch for Linear issue
make pr QNT=XX TITLE="..." # Push + create PR
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

Create `.claude/commands/` with workflow commands:

| Command | When | What |
|---|---|---|
| `/resume` | Start of session | Detect branch, pull Linear context, show recent work |
| `/cycle-start` | Start of week | Fetch cycle, list issues, suggest next pick |
| `/cycle-end` | End of week | Summarize shipped, roll over incomplete, velocity |
| `/sync-linear TEAM-XX` | Mid-session | Detect state (branch/PR/merged), update Linear |
| `/sanity-check TEAM-XX` | Before shipping | Lint + types + tests + acceptance criteria check |
| `/ship TEAM-XX` | Done with issue | Sanity check → PR → CI → merge → Linear Done → update phase checklist in project-requirement.md (`- [ ]` → `- [x]`) |
| `/retro` | End of milestone | Review velocity, capture lessons to memory, prep next |

### Command design principles
- Each command is self-contained markdown with clear step-by-step instructions
- Commands that take arguments use `$ARGUMENTS` placeholder
- Commands reference specific Linear team IDs and conventions from the project
- `/ship` should include the full pipeline including auto-merge for solo dev

---

## Phase 4: Working Docs

Create `docs/` as the project's shared brain:

```
docs/
├── INDEX.md                    # Navigation and purpose
├── project-requirement.md      # Full requirements (moved from root)
├── architecture/
│   └── system-overview.md      # How the system works, data flow
├── decisions/
│   ├── TEMPLATE.md             # ADR template
│   └── 001-first-decision.md   # Why X over Y
├── guides/
│   └── local-dev-setup.md      # Getting started from clone
└── api/
    └── *.http                  # REST Client test files
```

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
[ ] Linear: project + milestones + issues + cycles
[ ] Git repo initialized + GitHub remote
[ ] CLAUDE.md with project conventions
[ ] Makefile with dev commands
[ ] .env.example with all vars
[ ] .gitignore
[ ] .githooks/commit-msg
[ ] .claude/commands/ (7 slash commands)
[ ] docs/ structure (architecture, decisions, guides, api)
[ ] GitHub branch protection enabled
[ ] CI/CD workflows (at least CI)
[ ] Memory files saved for the project
[ ] First ADRs written for key decisions
```

Once all boxes are checked, run `/cycle-start` and begin building.
