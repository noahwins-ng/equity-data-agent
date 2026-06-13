"""Tests for the provider-pressure classifier (QNT-234).

The eval harness must tell a Groq capacity blow-up (quota / timeout / 5xx)
apart from a genuine app/routing/code regression so a QNT-233-style routing
fix is not blocked by free-tier provider pressure. These cover the
name-match, message-match, chain-walk, and (crucially) the negative cases
where a real code bug must NOT be misread as provider pressure.
"""

from __future__ import annotations

from agent.evals.provider_errors import is_provider_pressure_error, provider_error_label


# Locally-named stand-ins for the openai/LiteLLM exception classes -- the
# classifier matches on ``type(...).__name__``, so a class with the same name
# trips it without importing the SDK.
class APITimeoutError(Exception):
    pass


class RateLimitError(Exception):
    pass


class InternalServerError(Exception):
    pass


# httpx-style transport timeout, as raised when OUR report API is down -- the
# message contains "timed out" but it is NOT LLM provider pressure.
class ReadTimeout(Exception):
    pass


class TestIsProviderPressureError:
    def test_timeout_by_type_name(self) -> None:
        assert is_provider_pressure_error(APITimeoutError("Request timed out."))

    def test_rate_limit_by_type_name(self) -> None:
        assert is_provider_pressure_error(RateLimitError("anything"))

    def test_upstream_5xx_by_type_name(self) -> None:
        assert is_provider_pressure_error(InternalServerError("boom"))

    def test_message_match_through_bare_wrapper(self) -> None:
        # LiteLLM sometimes flattens the class to a bare Exception/ValueError;
        # the message still carries the provider signature.
        err = ValueError("litellm.RateLimitError: rate limit exceeded, quota for the day")
        assert is_provider_pressure_error(err)

    def test_quota_keyword_match(self) -> None:
        assert is_provider_pressure_error(RuntimeError("insufficient_quota: tokens per day"))

    def test_walks_cause_chain(self) -> None:
        try:
            try:
                raise RateLimitError("Error code: 429")
            except RateLimitError as inner:
                raise RuntimeError("comparison synthesis failed") from inner
        except RuntimeError as exc:
            assert is_provider_pressure_error(exc)

    def test_real_code_bug_is_not_provider_pressure(self) -> None:
        assert not is_provider_pressure_error(RuntimeError("graph broken"))
        assert not is_provider_pressure_error(KeyError("reports_by_ticker"))

    def test_context_window_is_not_provider_pressure(self) -> None:
        # An over-long prompt is an app / token-budget bug (AC5 territory), not
        # capacity -- it must stay a real failure that gates the suite.
        err = ValueError("ContextWindowExceededError: prompt exceeds the model limit")
        assert not is_provider_pressure_error(err)

    def test_report_api_transport_timeout_is_not_provider_pressure(self) -> None:
        # A timeout against OUR FastAPI report API (httpx ReadTimeout) is infra
        # being down, NOT LLM provider pressure -- it must still gate the suite,
        # not be silently excluded. The "timed out" / "timeout" message substrings
        # were dropped precisely so this is not misclassified (the openai SDK's
        # own APITimeoutError is still caught by type name).
        assert not is_provider_pressure_error(ReadTimeout("The read operation timed out"))
        assert not is_provider_pressure_error(TimeoutError("operation timed out"))


class TestProviderErrorLabel:
    def test_labels_recognised_type(self) -> None:
        assert provider_error_label(RateLimitError("x")) == "provider: RateLimitError"

    def test_labels_type_from_chain(self) -> None:
        try:
            try:
                raise APITimeoutError("Request timed out.")
            except APITimeoutError as inner:
                raise RuntimeError("wrapper") from inner
        except RuntimeError as exc:
            assert provider_error_label(exc) == "provider: APITimeoutError"

    def test_message_only_match_labels_outer_type(self) -> None:
        # Matched on message, not a known type name -> label the outermost type.
        err = ValueError("rate limit exceeded")
        assert provider_error_label(err) == "provider: ValueError"

    def test_empty_for_non_provider_error(self) -> None:
        assert provider_error_label(RuntimeError("graph broken")) == ""
