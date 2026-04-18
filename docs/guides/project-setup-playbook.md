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
- [ ] Create one **cross-cutting milestone** alongside the phase milestones: `Ops & Reliability` (or equivalent). Reactive incident follow-ups and production-hardening tickets land here — they don't fit cleanly into any one phase, and forcing them into Phase 0 obscures that they were learned the hard way, not planned. Issues like "CD verifies prod git SHA matches merged commit" or "add restart policy to prod services" belong here.

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

### 2.5 Docker Compose invariants (if using Docker for prod)

These are not optional polish — they are day-one invariants. Skipping them costs real production outages.

- **Every prod-profile service must declare `restart: unless-stopped`**. Docker's default restart policy is `no`, which means after a host reboot, nothing comes back up. The equity-data-agent project learned this on 2026-04-18 when a kernel-update reboot took the API dark for 48 minutes until manual `docker compose up -d` (see QNT-95). Chose `unless-stopped` over `always` because it still respects a manual `docker compose down` during deploys.
- **Named volumes for all stateful services.** ClickHouse, Postgres, Dagster run-history, Caddy certs — anything with state that must survive a container rebuild needs a named volume mounted into the service. Bind mounts from `./data/*` are brittle; named volumes are `docker volume inspect`-able.
- **Every service used by the app must be listed in a profile.** `profiles: [dev]` / `profiles: [prod]` gates what `docker compose up` brings up. Without profiles, all services start every time, which hurts local dev speed and obscures what prod actually runs.

### 2.6 Git hook (commit-msg)
Enforce two commit patterns:

```
# Code changes — must reference a Linear issue
TEAM-XX: type(scope): description

# Meta/process work — no issue needed (docs, commands, workflow tweaks)
docs: description
chore: description
```

The `docs:` / `chore:` exception covers ongoing maintenance like retro reports, plan syncs, and command updates that happen outside any specific issue. Without it, every workflow tweak needs a throwaway housekeeping issue.

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
| `/go TEAM-XX` | Work on an issue end-to-end | pick → implement → sanity-check → review → ship — fully automated, stops only on failure |
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
- `/cycle-start`, `/cycle-end`, and `/retro` post Linear project status updates — keeps the project feed as a lightweight audit trail without manual updates
- `/sanity-check` and `/change-scope` post comments on individual Linear issues for traceability

### Acceptance criteria taxonomy (learned the hard way in QNT-41, QNT-42, QNT-90)

Every AC falls into one of three classes, and `/sanity-check` must enforce the distinction. Conflating them is how "✓ AC met" ships broken code:

| Class | Verifiable by | Evidence required | Blocks ship? |
|---|---|---|---|
| **code AC** | Reading the implementation | None beyond pass/fail from the reviewer ("implemented in `foo.py:42`") | Yes if missing |
| **dev execution AC** | Running a command locally / on prod host | Command + output pasted inline — not "looks good", actual bytes | Yes if no evidence |
| **prod execution AC** | Only verifiable in the deployed environment | `⏳ PENDING` marker carried into `/ship`'s post-deploy step | No — defers to `/ship` |

**Keyword trigger.** If an AC contains "populated", "returns", "visible in", "deployed", "loaded", "CD passes", "sensor running", "schedule enabled", etc., it is *never* a code AC — those phrases are factual claims about runtime behavior that code inspection cannot prove. `/sanity-check` must hard-fail when a keyword-triggered AC lacks command+output evidence.

This taxonomy was codified in QNT-90 after three Phase 1 issues (QNT-41/42/43) were marked Done based on code inspection alone, then found broken when verified against prod. Ship `/sanity-check` with this logic baked in from day one.

### Implicit AC via diff-path triggers (`docs/AC-templates.md`)

Some AC apply to every PR in a class — not because the issue author remembered to add them, but because the class of change *demands* them. Keep these in `docs/AC-templates.md`, triggered by `git diff --name-only main...HEAD` matching a path glob:

```markdown
## Infra / CI / Deploy PRs

Apply when the diff touches any of: `docker-compose.yml`, `Dockerfile`, `.github/workflows/*.yml`,
`Makefile`, root config files, `scripts/*.sh` invoked by CD or the prod host.

### Default AC
- CD runs green end-to-end, including SHA + runtime-load verify gates
- No prod drift (`ssh prod 'cd /opt/app && git status --short'` returns empty)
- Post-deploy smoke: one cheap operation succeeds on prod (for a data pipeline: one asset materialization; for an API: one endpoint round-trip)
```

`/sanity-check` and `/review` both inspect the diff and append matching template AC to the per-issue list. This closes the gap where infra PRs "worked on my machine" but were never proven to actually deploy.

---

## Phase 4: Working Docs

Create `docs/` as the project's shared brain:

```
docs/
├── INDEX.md                    # Navigation and purpose
├── project-requirement.md      # Full requirements, architecture, stack
├── project-plan.md             # Phase-by-phase delivery checklists (synced via /ship + /sync-docs)
├── patterns.md                 # Established code recipes — read before implementing
├── AC-templates.md             # Implicit AC per diff-path trigger (see §3 above)
├── architecture/
│   └── system-overview.md      # How the system works, data flow, component responsibilities
├── decisions/
│   ├── TEMPLATE.md             # ADR template
│   └── 001-first-decision.md   # Why X over Y — include rejected alternatives
├── guides/
│   ├── dev-workflow.md         # Weekly cadence: how commands chain together
│   ├── local-dev-setup.md      # Getting started from clone
│   └── <cloud>-bootstrap.md    # One-time prod server setup (include any smarthost / mail-relay config)
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

**Non-negotiable CD hard gates** — the deploy is not "successful" until these pass. A 200 from `/health` on its own is not proof that the code you merged is the code that's running. Both gates were added after the 2026-04-16 outage, when CD reported green while prod was 17 commits behind main (a SCP'd hotfix had blocked `git pull`):

- [ ] **Prod SHA matches the merged commit**
  ```bash
  REMOTE_SHA=$(ssh prod "cd /opt/app && git rev-parse HEAD")
  MERGE_SHA=$(gh pr view <pr> --json mergeCommit --jq .mergeCommit.oid)
  [ "$REMOTE_SHA" = "$MERGE_SHA" ] || exit 1
  ```
  If these differ, the deploy did not land. Investigate `git status --short` on prod for SCP drift, then re-trigger CD. Don't run downstream checks on the wrong code.
- [ ] **Runtime loaded the expected code.** A container being "up" doesn't mean the definitions module loaded cleanly. For a data pipeline: assert the loaded asset / check / schedule counts match expected values. For an API: assert the expected route set is registered. A small Python `-c "..."` one-liner executed via `docker exec` works well.
- [ ] **Post-deploy smoke: one cheap real operation succeeds.** For a data pipeline: `dagster asset materialize --select <cheap_asset>`. For an API: `curl` a known-good endpoint against prod host. This catches runtime issues that a code-level import doesn't (credentials, DNS, network policies).

These three gates run *in CD itself* via GitHub Actions, and `/ship` re-verifies them at ship time in case CD was skipped or the branch raced with a drift.

### Observability (pick based on project)
- [ ] **Langfuse** — if using LLM agents (trace thoughts, tools, latency)
- [ ] **Sentry** — API error tracking (free tier: 5k errors/month)
- [ ] **Built-in UIs** — ClickHouse Play, Dagster UI, etc.

### Host-level reliability (VPS / bare-metal prod)

Three cheap signals that would have prevented both of this project's prod outages (Apr 16, Apr 18):

- [ ] **Cron health-monitor script** on the prod host, running every 15 min, writing to a log file. Checks API `/health`, `docker compose ps`, and `/var/run/reboot-required`. Writes a heartbeat file on success. Example: `scripts/health-monitor.sh` in equity-data-agent (~40 lines of bash).
- [ ] **Session-start hook in `.claude/hooks/session-start.sh`** that SSHs to prod with a 3-second timeout, tails the monitor's log, and injects any recent failures + pending reboots into the Claude Code session context. Makes prod health impossible to miss during dev work.
- [ ] **Mail alerts from `unattended-upgrades`** (Linux hosts) via an SMTP smarthost. Set `Unattended-Upgrade::Mail` and `MailReport "on-change"`. Verify end-to-end delivery before claiming this works.

**Smarthost choice for mail alerts** — we evaluated three, Resend was the winner:
- **Direct SMTP to recipient MX (port 25)** — rejected. Most cloud providers (incl. Hetzner) block outbound 25 by default, and consumer mail providers spam-filter VPS senders without SPF/DKIM.
- **Gmail SMTP relay (`smtp.gmail.com:587` + App Password)** — rejected. Requires 2FA + App Password on the Google account, which may not be available in the target environment.
- **Resend SMTP relay (`smtp.resend.com:587` + API key)** — chosen. Free tier (3k/month, 100/day), API-key auth, port 587 with STARTTLS so port-25 blocks are irrelevant. Full recipe in `docs/guides/<cloud>-bootstrap.md` §10 of the equity-data-agent repo.
- **Postfix gotcha**: `apt install bsd-mailx` with default debconf preseed sets `default_transport = error`, which silently bounces everything. Always run `postconf -e 'default_transport = smtp' 'relay_transport = smtp'` before testing delivery.

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

## Production Invariants from Day One

Short manifesto of rules this project paid to learn. Bake them into new projects before the first prod deploy, not after.

1. **Aggregate "green" hides invariants.** CI ✓ + CD ✓ + `/health` 200 does not prove your prod is in a state you can survive a failure from. Each signal proves a narrow claim (syntax, connectivity, responsiveness) — none proves durability. *Apr 16 outage: CD green while prod ran 17-commit-stale code. Apr 18 outage: /health 200 before the reboot, then 48 min dark because nothing restarted.*

2. **Chaos-test what you claim survivable.** If an AC says "survives host reboot", the acceptance criterion is `ssh prod 'sudo systemctl restart docker' && sleep 30 && assert_all_containers_up` — not "check returned 200". Run the actual failure injection during `/ship` post-deploy in a low-traffic window.

3. **Runtime state must be declarative.** Schedules, sensors, feature flags, restart policies — set them in code (config files / `default_status=RUNNING` / `restart: unless-stopped`). Never rely on "I toggled it in the UI that one time." A fresh deploy should reproduce the exact runtime state the docs describe. *QNT-92 lesson: sensors that were STARTED in the Dagster UI reverted to STOPPED on the next deploy, silently breaking the auto-recompute chain.*

4. **Restart policy is not optional polish — it's a day-one invariant.** `restart: unless-stopped` on every prod service in `docker-compose.yml` from the first deploy. Docker's default is `no`.

5. **Pending-reboot visibility before your first outage, not after.** Wire the `/var/run/reboot-required` check into the cron health-monitor + session-start hook before kernel updates become your problem. Mail alerts via Resend take ~10 minutes; the alternative is being surprised at 4 AM UTC.

6. **Document rejected alternatives, not just the chosen path.** When you pick Resend over Gmail over direct SMTP, the decision table lives in the docs forever. Future engineers (and future-you) re-derive the rejection every time the original rationale isn't written down. ADRs do this for architecture; bootstrap guides should do it for ops choices.

7. **Three-class AC taxonomy is load-bearing, not ceremony.** Code AC ≠ dev-execution AC ≠ prod-execution AC. Proving code is correct is not the same as proving the deployed system works. `/sanity-check` must enforce the distinction or it becomes theater.

8. **Retro reactive tickets are ~50% of true scope.** In this project's Phase 2, 6 planned issues shipped alongside 6 reactive follow-ups (QNT-87/88/89/90/91/92). Budget a retro-sweep cycle after every substantive phase. A milestone "complete" without a retro has undiscovered bugs, not zero bugs.

---

## Appendix: Data Pipeline Patterns

Applies if the project has batch ingestion → computed derivatives → query/API (e.g., Dagster + ClickHouse). Skip if not relevant.

- **Asset checks with real domain bounds, not "not null".** RSI must be between 0 and 100, volume must be > 0, P/E must be null when |EPS| < $0.10 (near-zero earnings). QNT-68 added 17 such checks to the equity-data-agent project; two of them caught actual formula bugs in fundamental ratios that code review had missed.
- **Sensor batching from day one.** If you write event-driven reactive assets (asset A materializes → sensor fires → asset B materializes), the sensor must batch *all* pending source events per tick. Single-event-per-tick processing can't catch up after a brief outage — it accumulates a backlog that grows until something hand-intervenes. *QNT-46 was rewritten once for exactly this reason.*
- **Sample AC broadly, across every dimension.** When spot-checking derived data, sample across every row type and timeframe (annual + quarterly; daily + weekly + monthly). QNT-45 shipped with only annual rows spot-checked; quarterly P/E was broken for 4 days until retro caught it.
- **`ReplacingMergeTree` everywhere + stable sort key = idempotent ingestion for free.** No manual dedup logic. Re-running the same partition overwrites cleanly.

---

## Checklist Summary

### Repo + workflow
```
[ ] Project brief written (docs/project-requirement.md)
[ ] Project plan written (docs/project-plan.md — phase checklists with ISSUE-XX references)
[ ] Linear: project + phase milestones + cross-cutting "Ops & Reliability" milestone + cycles
[ ] Git repo initialized + GitHub remote
[ ] CLAUDE.md with project conventions
[ ] Makefile with dev commands
[ ] .env.example with all vars
[ ] .gitignore
[ ] .githooks/commit-msg
[ ] .claude/commands/ (12 slash commands + three-class AC taxonomy enforced in /sanity-check)
[ ] docs/ structure (architecture, decisions, guides, retros, api)
[ ] docs/AC-templates.md (implicit AC per diff-path trigger)
[ ] docs/guides/dev-workflow.md (weekly cadence cheat sheet)
[ ] GitHub branch protection enabled
[ ] CI/CD workflows (at least CI)
[ ] Memory files saved for the project
[ ] First ADRs written for key decisions (include rejected alternatives)
```

### Production invariants (before first real deploy)
```
[ ] docker-compose.yml: every prod service declares `restart: unless-stopped`
[ ] Named volumes for all stateful services; services gated by prod/dev profiles
[ ] CD hard gate 1: prod git SHA == merged commit SHA
[ ] CD hard gate 2: runtime loaded expected code (asset graph / route set)
[ ] CD hard gate 3: post-deploy smoke — one cheap real operation succeeds on prod
[ ] scripts/health-monitor.sh cron installed on prod (15-min tick, logs failures + pending reboots)
[ ] .claude/hooks/session-start.sh tails the monitor log so prod failures surface in dev
[ ] Unattended-upgrades mail alerts via Resend SMTP (or equivalent smarthost); delivery verified end-to-end
[ ] Runtime state is declarative: schedules, sensors, feature flags set in code, not toggled in UI
[ ] Chaos-test AC: at least one acceptance criterion is a failure injection (`systemctl restart docker`, kill-pod, etc.) not just a liveness check
```

Once all boxes are checked, run `/cycle-start` and begin building.
