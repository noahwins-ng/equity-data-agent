"""Coverage tests for shared.tickers.TICKER_METADATA (QNT-236).

The runtime path asserts metadata coverage at module import via
``_validate_metadata_coverage(TICKERS, TICKER_METADATA)``; these tests give a
clean failure signal in CI and prove the validator actually fires when a
ticker is missing (or has incomplete) metadata — the failure mode the assert
exists to prevent.
"""

from __future__ import annotations

import pytest
from shared.tickers import (
    _REQUIRED_METADATA_KEYS,
    TICKER_METADATA,
    TICKERS,
    _validate_metadata_coverage,
)


def test_metadata_covers_every_ticker() -> None:
    """The real registry passes the validator (mirrors the module-load assert)."""
    _validate_metadata_coverage(TICKERS, TICKER_METADATA)


def test_validator_fires_on_missing_ticker() -> None:
    """A ticker with no metadata entry at all raises."""
    incomplete = {t: TICKER_METADATA[t] for t in TICKERS if t != TICKERS[0]}
    with pytest.raises(AssertionError, match=TICKERS[0]):
        _validate_metadata_coverage(TICKERS, incomplete)


def test_validator_fires_on_partial_keys() -> None:
    """A ticker present but missing required keys raises."""
    partial = dict(TICKER_METADATA)
    partial[TICKERS[0]] = {"name": "X", "sector": "Y", "industry": "Z"}
    with pytest.raises(AssertionError, match="missing required keys"):
        _validate_metadata_coverage(TICKERS, partial)


def test_required_keys_match_full_entry_shape() -> None:
    """Guard against the required-key set drifting from a real full entry."""
    assert _REQUIRED_METADATA_KEYS <= TICKER_METADATA[TICKERS[0]].keys()
