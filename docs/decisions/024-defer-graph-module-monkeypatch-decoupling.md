# ADR-024: Defer the graph.py re-export / monkeypatch decoupling; keep the module-object seam

**Date**: 2026-07-04
**Status**: Accepted

## Context

QNT-294 split `graph.py` into node modules (`agent.nodes.*`) plus pure-helper
modules (`agent.policy` / `agent.structured` / `agent.support`), but kept two
compat surfaces so the migration landed with zero test churn:

1. **Re-export block** - `graph.py` re-imports ~60 helper names (with
   `# noqa: F401`) from the helper modules, and every node body reaches them via
   the `graph` module object (`graph.get_llm()`, `graph.build_synthesis_prompt(...)`,
   `graph._confidence_from_reports(...)`, ...), i.e. attribute access at call time.
2. **Monkeypatch seam** - because nodes read helpers off the `graph` module
   object, tests patch one point (`setattr(graph_module, "get_llm", ...)`) and
   every node sees it. Measured coupling on this branch: **56** `graph_module.get_llm`
   patches + 3 others (`RETRIEVAL_SPECS`, `build_narrate_prompt`,
   `_runtime_grounding_check`), plus **~27** direct `from agent.graph import <helper>`
   imports across **16** test files.

QNT-307 retired the seven legacy answer slots (the other QNT-294 compat surface).
Its AC3 asks whether to also pay down this re-export/monkeypatch coupling now, and
explicitly allows deferring "with rationale - a working compat layer is not
automatically debt worth paying down."

## Decision

**Defer the decoupling. Keep the `graph` module-object seam as-is.** Nodes keep
reaching helpers via `graph.<name>`; tests keep patching `graph_module.<name>`;
the re-export block stays (documented with `# noqa: F401`).

The re-export/monkeypatch coupling is a **working, deliberate test seam**, not a
bug or a runtime cost - the AC itself notes "that indirection is not a runtime
need." Paying it down is pure test-mechanics churn with real regression risk and
no runtime change, so it does not clear the bar this cycle. The slot retirement
(QNT-307 AC1/AC2) - which *does* simplify the runtime state shape and single-writer
contract - is the debt worth paying here; it is independent of this seam and ships
on its own.

**The rule this records** (so a future re-raise resolves against it):

> A module-object indirection that exists solely to give tests one patch point is
> a legitimate seam, not automatically debt. Decouple it only when it starts
> *hiding* a real problem - an import cycle, a test-isolation failure, or a node
> that needs a helper the re-export doesn't cover - not on taste or block size.

## Alternatives Considered

* **Do the full decoupling now** (the AC's "if done" path). Rejected this cycle.
  The dominant seam, `graph.get_llm` (56 sites), cannot be migrated by simply
  patching `agent.llm.get_llm`: `graph.py` re-imported the *name*, so
  `graph.get_llm` is a binding distinct from `agent.llm.get_llm`, and patching the
  latter would not affect nodes that call `graph.get_llm()`. Making the migration
  correct means rewriting every `graph.<helper>` call across all node modules to
  attribute-access the true owner (`import agent.llm as llm; llm.get_llm()`, and
  likewise for `agent.prompts` / `agent.support` / `agent.policy` / `agent.structured`),
  then swapping all 56 patches plus the ~27 direct imports. That is the *same*
  indirection shape pointed at a different module - ~80+ edits across 16 test
  files and every node, for zero runtime change and a nontrivial chance of
  breaking test isolation. Net near-even at best; the only gain is cosmetic
  ("patch the source, not the re-export").
* **Introduce one small seam module both nodes and tests target** (the AC's other
  "if done" option). Rejected: it adds a new module whose only job is to be a
  patch point - replacing the existing `graph`-as-seam with a purpose-built seam.
  Same indirection, new surface, still ~56 patch-site edits. No net simplification.
* **Partial: migrate only `get_llm`** off `graph_module`. Rejected: leaves the
  re-export block and the `graph.<helper>` node idiom for the other ~59 names, so
  the "block shrinks to external-only names" AC outcome is not reached anyway -
  churn without closing the surface.

## Consequences

**Easier**

* The QNT-307 diff stays surgical: the slot retirement lands without dragging in
  an ~80-edit test-mechanics rewrite and its regression risk. Full suite stays
  green on the change that matters.
* The seam question is closed against a written rule; a future re-raise resolves
  against it rather than a blank slate.

**Harder / watch**

* The `graph.py` re-export block stays large (~60 `# noqa: F401` names) and the
  `graph.<helper>` node idiom persists - a reader still learns the helpers live
  elsewhere only by following the re-import. Accepted: the module docstring and
  the QNT-294 relocation comment already point at the real owners
  (`agent.policy` / `agent.structured` / `agent.support`).
* Revisit if the seam starts hiding a real problem (import cycle, test-isolation
  failure, or a node needing an un-re-exported helper), per the rule above.

## Acceptance-criteria status (QNT-307 AC3)

* **AC3 (re-export / monkeypatch decision recorded)** - **Defer**, with the
  rationale above. The "if done" clauses (nodes import helpers directly, block
  shrinks to external-only, no test patches a self-called seam) are intentionally
  **not** pursued this cycle. ✓
