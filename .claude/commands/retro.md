# Retrospective

End-of-milestone retrospective. Reviews what happened across all cycles in the current or specified milestone, captures lessons, and preps for the next phase.

Optional argument: milestone name (e.g., `/retro Phase 0: Foundation`). If omitted, uses the most recently completed milestone.

The argument is: $ARGUMENTS

## Instructions

### Step 1: Gather Data
1. **Identify the milestone** — from the argument or find the most recently completed one in Linear
2. **List all issues** in the milestone with: title, status, how many cycles it took, PR link
3. **Check git history** — `git log --oneline` for commits related to these issues
4. **Note the timeline** — when did the first issue start? When did the last one close?

### Step 2: Analyze
1. **What shipped** — count of issues closed, list of features/deliverables
2. **Velocity** — issues per cycle, any that took longer than expected
3. **Surprises** — anything that was harder or easier than expected, based on:
   - Issues that were reopened or had multiple PRs
   - Issues that were descoped or split
   - Commits that suggest debugging or rework
4. **Blockers** — anything that stalled progress (external APIs, tooling issues, etc.)

### Step 3: Capture Lessons
For each non-obvious lesson learned:
- Save it to memory (feedback type) so future sessions benefit from it
- Focus on: what to repeat, what to avoid, what surprised us

### Step 4: Prep Next Phase
1. Show the next milestone and its issues
2. Flag any issues that might be risky or underspecified based on lessons learned
3. Suggest which issues to pull into the next cycle

### Step 5: Report
Format as:

```
Retrospective: Phase X — Title
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Timeline: <start> → <end> (N cycles)
Shipped:  X issues, Y PRs merged

What went well:
  - ...
  - ...

What was harder than expected:
  - ...

Lessons saved to memory:
  - ...

Next up: Phase Y — Title (Z issues)
```
