# Project-level subagents

Project-specific subagent definitions for the `equity-data-agent` workflow. These supplement (not replace) the built-in Claude Code subagents (`Explore`, `general-purpose`, `Plan`, etc.).

## Active agents

| Agent | Purpose | Invoked by |
|---|---|---|
| `code-reviewer-ediff` | Adversarial review of a branch diff with repo rules baked in | `/review` (automatic), or manual |

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

1. Delete the agent file:
   ```bash
   rm .claude/agents/code-reviewer-ediff.md
   ```
2. Revert the `/review` slash command change. Open `.claude/commands/review.md` and remove the block added under "Step 2: Review (Adversarial)" that begins with `### Step 2.0: Spawn code-reviewer-ediff subagent`. The original Step 2 begins with "Read the full diff and review it as if you did NOT write this code." — restore that as the first sub-item.
3. If no other agents exist, remove the directory:
   ```bash
   rmdir .claude/agents
   # Keep this README.md only if more agents are planned; otherwise:
   rm .claude/agents/README.md && rmdir .claude/agents
   ```
4. Commit as `chore: remove code-reviewer-ediff experiment` with a note on why (e.g. "no BLOCKING catches over Cycle 2").

### Path C: soft-disable without deleting

If you want to pause the experiment but keep the file for future revival:

1. Edit `.claude/commands/review.md` and comment out the "Spawn subagent" step (wrap in `<!-- disabled YYYY-MM-DD: reason -->`).
2. Leave `.claude/agents/code-reviewer-ediff.md` in place. The agent won't fire without the /review invocation.
3. No commit needed if you're the only user; commit as `chore` if working with others.

### What doesn't need to be undone

- Nothing outside `.claude/agents/` and `.claude/commands/review.md` was changed.
- No Linear tickets, docs, or source code were touched.
- No prod config, no CD workflow changes.

The experiment is fully local to the Claude Code harness config.
