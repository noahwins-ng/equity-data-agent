"""Golden-set regression harness (QNT-67, eval type (b)).

Runs the agent against 15-20 curated (ticker, question, reference_thesis,
expected_tools) pairs stored in goldens/questions.yaml. Per run, scores each
generated thesis with an LLM-as-judge rubric and cosine similarity to the
reference thesis, then appends one row per record to evals/history.csv so
prompt-version quality is visible in git log.

Implementation deferred to Phase 5. See README.md.
"""
