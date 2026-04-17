"""Numeric-claim hallucination detector (QNT-67, eval type (a)).

Regex every number out of the agent's thesis; assert each appears verbatim in
one of the report strings the agent received as tool output. Any mismatch is a
hallucination — direct operational enforcement of the ADR-003 contract.

Implementation deferred to Phase 5. See README.md.
"""
