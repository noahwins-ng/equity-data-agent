# Change Scope

Formalise a requirement change: update the spec and Linear. Handles additions, drops, and modifications.

Pass the change type and description as arguments (e.g., `/change-scope drop QNT-52 — switching to RSS instead of paid news API`).

The argument is: $ARGUMENTS

**Scope of this command**: spec (`project-requirement.md`) + `system-overview.md` + Linear only.
`project-plan.md` is NOT touched here — run `/sync-docs` after to reconcile the plan.

---

## Instructions

### Step 1: Identify Change Type
Determine from the argument whether this is:
- **add** — new requirement being introduced
- **drop** — existing requirement being removed
- **modify** — existing requirement changing scope or approach

If unclear from the argument, ask before proceeding.

### Step 2: Update `docs/project-requirement.md`

**add**: Insert the new requirement into the appropriate phase/section. Define the *what* and *why* — not just the deliverable but the rationale and any constraints.

**drop**: Remove or clearly mark the requirement as dropped. If it was part of a larger section, update surrounding context so the spec still reads coherently.

**modify**: Update the affected section. Be precise — change only what changed. If the rationale shifted, update that too.

### Step 2b: Update `docs/architecture/system-overview.md`
If the change affects any of the following, update the relevant section of `system-overview.md`:
- Data flow (new sources, new Dagster assets, new DB tables)
- Component responsibilities (new package, changed layer boundaries)
- API surface (new or removed endpoints, changed request/response shapes)
- Infrastructure (new services, changed resource limits, new env vars)

If none of the above apply, skip this step.

### Step 2c: Update `docs/project-plan.md`

**add**: Draft a new plan entry for the issue and insert it into the correct phase section. Format it to match surrounding entries (checkbox, QNT-XX reference, sub-bullets for deliverables if applicable). Do NOT wait for `/sync-docs` — add it now.

**drop**: Remove the corresponding entry from the plan. If it has sub-bullets or context, remove those too so the section still reads coherently.

**modify**: Find the plan entry matching this issue (by QNT-XX reference) and update its text to reflect the new scope. Update sub-bullets if the deliverables changed. `/sync-docs` only handles status (Done/Cancelled) — it will not update text.

### Step 3: Update Linear

**add**: Create a new Linear issue in the Quant team.
- Title, description, acceptance criteria
- Assign to correct milestone/phase
- Set priority
- Project: Equity Data Agent

**drop**: Cancel the corresponding Linear issue.

**modify**: Update the Linear issue description and acceptance criteria to reflect the new scope.

### Step 3b: Log activity on the issue
Post a comment on the affected Linear issue (for all three change types) to create a permanent audit trail:

```
**Scope change [add | drop | modify]** — YYYY-MM-DD

What changed: <one-line summary>
Reason: <from the user's argument>
Spec: docs/project-requirement.md — <section>
ADR: docs/decisions/00N-title.md | none
```

### Step 4: ADR Check
Assess whether the change warrants an Architecture Decision Record:
- Does it change the system's architecture, data flow, or component responsibilities?
- Would a future developer wonder "why was X changed/dropped/added?"
- Does it affect multiple phases or components?

If yes → create a new ADR in `docs/decisions/` using `TEMPLATE.md`. Number it sequentially after the last existing one. Add it to `docs/INDEX.md` under the decisions section.

If no → skip.

### Step 5: Report

```
Scope change: [add | drop | modify]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Spec:     Updated docs/project-requirement.md — <section>
Overview: Updated docs/architecture/system-overview.md — <section> | none
Plan:     Updated docs/project-plan.md — <entry added | removed | text updated> | none
Linear:   QNT-XX [created | cancelled | updated] — <title>
ADR:      docs/decisions/00N-<title>.md (if applicable) | none

Plan updated inline. Run /sync-docs only if you also need to reconcile
unrelated Done/Cancelled issues that drifted since the last sync.
```
