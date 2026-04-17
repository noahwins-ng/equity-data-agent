"""Tool-call correctness (QNT-67, eval type (c)).

For each golden-set question, assert the expected tool was called — e.g.,
valuation questions MUST call get_fundamental_report, technical questions MUST
call get_technical_report. Catches prompt regressions that cause the agent to
grab the wrong reports.

Implementation deferred to Phase 5. See README.md.
"""
