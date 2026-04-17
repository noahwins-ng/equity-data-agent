# ADR-007: Minimal agent graph first

**Date**: 2026-04-17
**Status**: Accepted

## Context

LangGraph makes it cheap to add nodes. A healthy-looking "agentic" graph often grows to 5–7 nodes: plan → gather → analyse → critique → reflect → retry → synthesise. Each extra node compounds latency, token cost, trace noise, and the surface area the eval harness has to cover. It also makes regressions harder to attribute — when the thesis gets worse, which node caused it?

Without an eval harness, adding nodes is indistinguishable from the graph "working better" because the only feedback signal is the author's gut. ADR-003 already enforces that the agent never does math; the remaining failure mode is reasoning quality, and that can only be measured (see QNT-67).

## Decision

The initial LangGraph agent ships with exactly **three nodes**:

```
plan → gather → synthesize
```

- **plan** — decide which tools to call for this `(ticker, question)` pair
- **gather** — invoke the tools (summary/technical/fundamental/news report + news search as needed) in parallel where possible
- **synthesize** — compose the final thesis from the gathered report strings

There is **no critique, reflect, retry, or re-plan node** in the initial graph. Additional nodes are added only when the QNT-67 eval harness surfaces a specific, reproducible failure mode that a new node would fix — and the new node's effect is verified against the same harness.

## Alternatives Considered

**Ship the common 5–7 node graph (plan → gather → analyse → critique → reflect → synthesise)**
- Looks more "agentic" in architecture diagrams.
- Rejected: without evals, there's no way to tell if critique/reflection are improving thesis quality or just burning tokens. Every extra node also multiplies Langfuse trace size, making manual trace review harder.

**Single-node ReAct loop with tool-calling in one node**
- Simpler than 3 nodes, and many frameworks default to this.
- Rejected: fuses planning and gathering into one blob, which makes it hard to enforce that `plan` decides tools deterministically. The 3-node split keeps each concern inspectable in a Langfuse trace.

**Skip LangGraph entirely, use raw tool-use loop**
- Even simpler, no framework.
- Rejected: LangGraph's state primitive, streaming, and observability hooks (Langfuse) are exactly what the SSE endpoint needs. The cost is minimal; the benefit is real.

## Consequences

**Easier:**
- Smallest possible eval surface area — 3 nodes to trace, debug, and regression-test.
- Langfuse traces are readable at a glance; failure attribution is obvious.
- Cheaper and faster runs during prompt iteration (fewer tokens per thesis).
- New nodes are added *with* evidence, not *hoping for* evidence.

**Harder:**
- The agent can't self-correct mid-run — a bad plan produces a bad thesis with no second chance in the same invocation. (Mitigation: the QNT-67 hallucination detector catches the one failure mode that matters most; other failures are caught by golden-set regression before deploy.)
- No implicit "reasoning trace" from a reflect node. (Mitigation: the synthesize node's prompt is structured to produce the reasoning inline in the thesis.)

## Revisiting

Re-open this ADR when the QNT-67 golden-set shows a failure pattern where:
1. The failure is consistent (not flaky), and
2. The failure is plausibly addressable by an additional node, and
3. A prototype of that node measurably improves the relevant eval metric without regressing others.

Adding a node without all three is scope creep.
