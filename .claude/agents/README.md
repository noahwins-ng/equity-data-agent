# Project-level subagents

Project-specific subagent definitions for the `equity-data-agent` workflow. These supplement (not replace) the built-in Claude Code subagents (`Explore`, `general-purpose`, `Plan`, etc.).

## Active agents

| Agent | Purpose | Invoked by |
|---|---|---|
| `code-reviewer-ediff` | Adversarial review of a branch diff with repo rules baked in | `/review` (automatic), or manual |
| `ops-investigator` | Triage prod incidents: match symptoms against the runbook, return hypothesis + next-step commands | Manual — when prod misbehaves |

## Invocation

Subagents defined in this directory are auto-discovered by the `Agent` tool. Invoke via:

```
Agent(
  subagent_type: "code-reviewer-ediff",
  description: "Adversarial review of PR #N",
  prompt: "Review PR #N. AC: <pasted from Linear>. Diff: run `gh pr diff N`."
)
```

## Experiment log

### `code-reviewer-ediff` — started 2026-04-21

**Hypothesis**: an independent adversarial reviewer that has not seen the writing process will catch categorial bugs (boundary violations, missing AC evidence, rule mismatches) that the author-turned-reviewer misses due to anchoring bias.

**Success criterion**: over the next cycle, the subagent catches ≥1 real BLOCKING issue during `/review` that I would have missed in a direct review. If so, keep. If not, delete.

**Review check-in date**: end of Cycle 2 (2026-04-26) or first post-`/review` observation, whichever comes first.

### `ops-investigator` — started 2026-04-21

**Hypothesis**: delegating prod incident triage (ssh-heavy, multi-signal cross-reference, runbook matching) to a focused subagent returns a better triage in less time than I can produce inline, and keeps the main session context free of dozens of raw command outputs.

**Calibration data**: the 2026-04-20 QNT-113 incident (Dagster backfill fan-out OOM). Prompt was written with that incident's actual diagnostic flow in mind — container state + kernel logs + daemon logs + cgroup cross-reference — to ensure the agent doesn't single-signal-misdiagnose the way a naive pass would (the Discord `[OOM KILL] dagster-daemon` header alone suggests daemon crash; truth was child subprocesses).

**Success criterion**: over the next two incidents on prod, the subagent's class match matches the eventual root cause AND its "next steps" list contains the commands I actually end up running. If it whiffs on either, revise the prompt or remove.

**Review check-in date**: whenever the second real incident after 2026-04-21 closes. (If no incidents fire in 30 days, revisit whether the agent is worth maintaining vs. the inline triage approach.)

---

## Undo guide — how to remove this experiment cleanly

If the experiment fails or the pattern isn't worth keeping, revert with one of the paths below.

### Path A: revert the introduction PR (simplest)

If the introduction PR is still the most recent merge on `main`:

```bash
git checkout main && git pull
git revert <introduction-PR-merge-SHA>
git push origin HEAD:refs/heads/chore/revert-agent-experiment
gh pr create --title "chore: revert code-reviewer-ediff experiment" \
  --body "Experiment did not meet success criterion. Reverting per .claude/agents/README.md undo guide."
```

### Path B: manual removal (if other commits have landed since)

Remove one or both agents independently — they're not coupled.

**`code-reviewer-ediff`:**
1. Delete the agent file: `rm .claude/agents/code-reviewer-ediff.md`
2. Revert the `/review` slash command change. Open `.claude/commands/review.md` and remove the block added under "Step 2: Review (Adversarial)" that begins with `**Step 2.0: Spawn code-reviewer-ediff subagent first**`. The original Step 2 begins with "Read the full diff and review it as if you did NOT write this code." — restore that as the first sub-item (remove the `Step 2.1:` prefix too).

**`ops-investigator`:**
1. Delete the agent file: `rm .claude/agents/ops-investigator.md`
2. No slash command references it (invoked manually only), so no other edits required.

**Final cleanup (if removing all agents):**
```bash
# Keep this README.md only if more agents are planned; otherwise:
rm .claude/agents/README.md && rmdir .claude/agents
```

Commit as `chore: remove <agent-name> experiment` with a note on why (e.g. "no BLOCKING catches over Cycle 2" or "whiffed on last two incidents").

### Path C: soft-disable without deleting

If you want to pause an experiment but keep the file for future revival:

**`code-reviewer-ediff`:** Edit `.claude/commands/review.md` and comment out the "Spawn subagent" step (wrap in `<!-- disabled YYYY-MM-DD: reason -->`). The agent file stays in place; it won't fire without the `/review` invocation.

**`ops-investigator`:** Rename the file to `.claude/agents/ops-investigator.md.disabled`. Claude Code auto-discovery reads only `.md`, so the renamed file is invisible to the `Agent` tool but preserved on disk. Restore by renaming back.

No commit needed if you're the only user; commit as `chore: disable <agent> experiment` if working with others.

### What doesn't need to be undone

- Nothing outside `.claude/agents/` and `.claude/commands/review.md` was changed.
- No Linear tickets, docs, or source code were touched.
- No prod config, no CD workflow changes.

The experiment is fully local to the Claude Code harness config.
