# Go

Full end-to-end orchestrator for a single issue. Invokes each step as a sub-command so every step gets its full instructions loaded. Pass the issue identifier as an argument (e.g., `/go QNT-40`).

The issue identifier is: $ARGUMENTS

## Instructions

Run each step in sequence by **invoking the actual slash command** via the Skill tool. Do NOT re-implement sub-command logic inline — the whole point is that each command's full prompt loads fresh with all its rules and checklists.

**On failure at any step**: diagnose, fix, and re-invoke the failed step. Only ask the user after two failed attempts at the same step.

---

### Step 1: Pick

Invoke `/pick` with the issue identifier via the Skill tool.

Wait for it to complete. Confirm the output shows:
- Branch checked out
- Linear → In Progress
- Acceptance criteria listed

If blocked by another issue, stop and warn the user.

---

### Step 2: Implement

Invoke `/implement` with the issue identifier via the Skill tool.

Wait for it to complete. Confirm the output shows:
- All ACs marked DONE (or NEEDS MANUAL VERIFICATION with reason)
- Lint, format, types passed
- WIP commits created

If any ACs are PARTIAL, re-invoke `/implement` to finish them.

---

### Step 3: Sanity Check

Invoke `/sanity-check` with the issue identifier via the Skill tool.

**This is a hard gate.** Wait for it to complete and read the verdict:
- **READY TO SHIP**: proceed to Step 4
- **NEEDS FIXES**: fix the issues, then re-invoke `/sanity-check` (do NOT skip to ship)

Do NOT proceed past this step unless the sanity check verdict is READY TO SHIP.

---

### Step 4: Review

Invoke `/review` with the issue identifier via the Skill tool.

Wait for it to complete and read the verdict:
- **SHIP**: proceed to Step 5
- **FIX FIRST**: fix the blocking issues, then re-invoke `/review`

---

### Step 5: Ship

Invoke `/ship` with the issue identifier via the Skill tool.

Wait for it to complete. Confirm the output shows:
- PR created/merged
- Linear → Done (or blocked on prod verification)

---

### Step 6: Report

After all steps complete, output the final summary:

```
Done: QNT-XX — Title
━━━━━━━━━━━━━━━━━━━━
PR:     <url> (merged)
Status: Done
Branch: deleted

Acceptance Criteria:
  ✓ Criterion 1
  ✓ Criterion 2
  ✓ Criterion 3

Pipeline: pick ✓ → implement ✓ → sanity-check ✓ → review ✓ → ship ✓

Milestone: Phase X — Y% complete
Next up:   QNT-YY — <title>  (run /go QNT-YY to continue)
```

---

## Error Recovery

**Tracking attempts**: After each failed fix attempt, create a WIP commit:
```
QNT-XX: wip: fix attempt — <what was tried and why it failed>
```
To check how many attempts have been made at the current step, count commits matching `fix attempt` in `git log --oneline main...HEAD`. This is durable across context compressions.

If any step fails and cannot be auto-fixed after 2 attempts (i.e., 2 "fix attempt" commits for the same step):

1. **Commit a WIP checkpoint** of any progress made so far
2. **Report what failed** with the specific error
3. **Suggest `/fix`** — e.g., "Run `/fix QNT-XX` after resolving the issue to resume the pipeline"
4. **Note which step to resume from** — so `/fix` knows where to pick up

### Detecting sanity-check flapping

If `/sanity-check` returns different verdicts (READY TO SHIP ↔ NEEDS FIXES) on two consecutive invocations at the **same branch tip SHA** (i.e., no new commits between runs), the disagreement is not about the code — something upstream changed between runs (prod state, CD pipeline, tunnel availability, Linear data, a running process stopping, etc.).

When this happens:

1. Do NOT re-invoke `/sanity-check` a third time — retrying doesn't fix non-deterministic inputs.
2. Pause and ask the user, reporting both verdicts, the SHA, and what looked different between the two runs (e.g., "tunnel was up for run #1, down for run #2").

Track each run's SHA + verdict in your session context (or by re-reading the prior `/sanity-check` report in the conversation) — do NOT record them as commits, because a tracking commit would change the branch tip SHA and defeat the same-SHA detection.
