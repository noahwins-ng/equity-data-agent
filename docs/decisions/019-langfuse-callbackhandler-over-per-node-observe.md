# ADR-019: Langfuse instrumentation via LangGraph CallbackHandler, not per-node @observe

**Date**: 2026-05-09
**Status**: Accepted

## Context

Phase-5 instrumentation hand-decorated every graph node with `@observe(name=...)` and routed every LLM call through a custom `LangfuseResource.traced_invoke()` wrapper. The wrapper opened an explicit `start_as_current_observation(as_type="generation", ...)` per call, captured prompt / completion / model_name / token usage, and tagged failed spans with `level="ERROR"`. The pattern was internally consistent and let an AST scanner enforce "no raw `llm.invoke()`" at lint time, but it diverged from Langfuse's documented LangGraph integration.

In May 2026 the project hit Langfuse's 50k events / month free-tier ceiling. PR #259 cut volume by env-stripping Langfuse during eval bench runs and dropping `@observe` from the six tool wrappers (HTTP-to-FastAPI calls, not LLM calls). Per-chat observation count dropped from ~12 to ~6. The next structural lever was either to self-host Langfuse on Hetzner (operational surface, ~512 MB extra RAM) or to align with the Langfuse-LangGraph cookbook so we could turn the `sample_rate` knob and tag traces with `session_id` / `user_id` for filterable views.

The cookbook pattern is: open a parent trace at the request boundary with `@observe()`, attach a single `CallbackHandler` to the graph at entry via `config={"callbacks": [handler]}`, and let LangGraph propagate the runnable config to every node + every nested LangChain runnable. Generation observations land automatically when each node forwards `config` into its inner `llm.invoke(prompt, config=config)` call.

## Decision

Replace the per-node `@observe` decorator forest and the `traced_invoke` wrapper with a single `CallbackHandler` attached at the graph entry point. Concretely:

1. `agent.tracing` shrinks to a thin client + a `make_callback_handler()` factory + re-exports of `observe`, `propagate_attributes`, `get_client` from the Langfuse SDK. `LANGFUSE_SAMPLE_RATE` (default 1.0; prod will set 0.2) is threaded into the `Langfuse(sample_rate=...)` constructor.
2. Every graph node accepts `(state: AgentState, config: RunnableConfig)`. Inner `llm.invoke()` calls pass `config=config` so the CallbackHandler attached at graph entry propagates through.
3. The FastAPI `agent_chat._runner` keeps `@observe(name="agent-chat")` as the parent-trace primitive, wraps the `graph.invoke(...)` call with `propagate_attributes(trace_name="agent-chat", session_id=<uuid4>, user_id=sha256(client_ip)[:12])`, and passes `config={"callbacks": [make_callback_handler()]}` to the graph. The CLI `agent.__main__.analyze` follows the same pattern minus session/user tagging (single-shot, no IP).
4. The architectural invariant moves from "every LLM call goes through `traced_invoke`" to "every LLM call passes `config=` so callbacks propagate". An AST scanner in `tests/agent/test_tracing.py::test_llm_invoke_calls_pass_config_kwarg` enforces it at lint time; a runtime test asserts the kwarg lands on the structured-output stub.
5. The eval judge drops `traced_invoke` and calls `judge_llm.invoke(prompt)` directly. Eval bench runs env-strip Langfuse keys at `evals/__main__.py` import time so callbacks have no client to flow to anyway.

Net code reduction: `tracing.py` from ~160 to ~68 lines; ~13 `@observe` decorators / `traced_invoke` call sites collapse into the single graph-entry hook.

## Alternatives Considered

* **Keep the per-node `@observe` pattern, sample at the SDK level only.** Cheapest diff, but the structural divergence from the cookbook stays. Future engineers reading the repo would carry the maintenance burden of explaining "why we don't use the documented pattern". Also misses the `propagate_attributes` channel for filterable session/user tags.
* **Self-host Langfuse on Hetzner.** Free events forever, trades infra cost for an extra container in the obs-smoke gate (~512 MB RAM, another failure surface — `feedback_health_endpoint_is_not_durability.md` is the cautionary tale). Worth doing if `sample_rate=0.2` is still insufficient, not before.
* **Move to Langfuse paid tier ($59/mo for 100k events).** Solves the symptom but bypasses the cookbook alignment. Skipped: the cookbook migration is a single-PR diff and unlocks the cost-control knob for free.

## Consequences

* **Easier**: idiomatic alignment with Langfuse's documented LangGraph pattern; structured-output retries, prompt-template chains, and tool bindings are auto-traced (we currently miss those); session-id and user-id filters work in the Langfuse UI; `sample_rate` is the long-term cost lever; ~130 LoC removed from the tracing layer; future engineers find the pattern in the cookbook instead of having to read our wrapper.
* **Harder**: the AST contract test had to be reframed. The new invariant ("`config` kwarg present") is mechanical to enforce statically but more abstract than "routes through `traced_invoke`". Test fixtures that monkeypatched `langfuse.traced_invoke` migrated to monkeypatching `llm.invoke` directly — straightforward but touched ~10 sites.
* **One filter-cardinality decision baked in**: per-IP `user_id` is `sha256(client_ip)[:12]`, not the raw IP. The hash is for *stable filter cardinality* in the Langfuse UI ("all traces from this IP" is one filter click) and to keep raw IPs out of observability tooling out of habit, **not** for privacy — there is no auth and no PII in this app, the IP is not sensitive (city-level geo at most, which any HTTP server already logs). An earlier code-review round added an HMAC + `LANGFUSE_USER_ID_PEPPER` setting to make the hash inversion-resistant; we reverted that on a follow-up review pass after agreeing the threat doesn't justify the env-var overhead in an open public-demo. Revisit-when triggers: if login/multi-tenant lands, swap to HMAC + a SOPS-encrypted pepper *before* `user_id` maps to a real identity.
* **`traced_invoke`'s explicit model-name + token-usage capture is replaced by the CallbackHandler's automatic capture.** The cookbook claims parity; pre-merge smoke trace verifies a chat lands `model_name` + `usage_details` on every generation observation. If a future SDK update breaks parity, we'd add `metadata={"model": ...}` at the LangChain LLM constructor as the workaround.
* **Failure-tagging shifts from explicit (`gen.update(level="ERROR", ...)` in `traced_invoke`) to implicit (LangChain's runnable error handling raises through the callback).** Generation observations on failed runs still surface with the exception in the Langfuse UI; the explicit-tag path is no longer needed.

## References

* Langfuse-LangGraph cookbook: `https://langfuse.com/guides/cookbook/example_langgraph_agents`
* Langfuse-LangChain integration docs: `https://langfuse.com/integrations/frameworks/langchain` (covers `propagate_attributes` + `CallbackHandler` parent-trace pattern)
* Predecessor: PR #259 (Tier-1 cuts: env-strip evals + drop tool `@observe`)
* Memory: `feedback_quality_before_capacity_in_fallback.md` (selection-by-quality first, capacity second — sample_rate is the right capacity lever post-quality-pass)
