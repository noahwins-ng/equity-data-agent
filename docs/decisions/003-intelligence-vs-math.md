# ADR-003: Strict separation between calculation and reasoning layers

**Date**: 2026-04-12
**Status**: Accepted

## Context
AI agents applied to financial analysis frequently hallucinate numbers — fabricating RSI values, inventing percentage changes, or miscalculating ratios. This undermines trust in the output.

## Decision
Enforce a hard architectural boundary: the LLM receives only pre-computed, human-readable report strings. It never performs arithmetic. All math is done by Python/SQL in the Dagster and FastAPI layers.

## Alternatives Considered
- **Give the agent a calculator tool**: Let the LLM call a calculation function when it needs math. Risk: the agent may still attempt mental math or pass incorrect inputs to the calculator.
- **Trust the model with simple math**: Modern LLMs are better at arithmetic. Risk: "better" is not "reliable" — a single hallucinated P/E ratio destroys thesis credibility.

## Consequences
- **Positive**: Every number in the thesis is traceable to a pre-computed source. Hallucination of financial data is architecturally impossible. Reports can be validated independently of the agent.
- **Negative**: The agent can't perform ad-hoc analysis (e.g., "what if revenue grows 10% next quarter?"). Every calculation the agent might need must be pre-computed.
- **Mitigated by**: Designing comprehensive report templates (QNT-69) that anticipate what the agent needs. If new calculations are needed, they're added as Dagster assets — not as agent capabilities.
