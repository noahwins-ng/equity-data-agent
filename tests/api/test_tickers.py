"""Tests for /api/v1/tickers — the ticker registry endpoint the frontend
selector depends on.

No DB round-trip: this endpoint reads shared.tickers.TICKERS in-process, so
the test just asserts the response mirrors the registry exactly (same values,
same order) — that invariant is what lets the frontend avoid hardcoding.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from api.main import app
from fastapi.testclient import TestClient
from shared.tickers import TICKERS


@pytest.fixture
def client() -> Iterable[TestClient]:
    with TestClient(app) as c:
        yield c


def test_tickers_returns_registry_in_order(client: TestClient) -> None:
    r = client.get("/api/v1/tickers")
    assert r.status_code == 200
    assert r.json() == list(TICKERS)
