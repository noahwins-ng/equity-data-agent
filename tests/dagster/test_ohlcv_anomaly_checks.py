from __future__ import annotations

from datetime import date, timedelta
from typing import cast

import pandas as pd
from dagster_pipelines.asset_checks.ohlcv_raw_checks import (
    _price_gap_anomalies,
    _volume_spike_anomalies,
)


def test_volume_spike_anomaly_fires_on_synthetic_spike() -> None:
    start = date(2026, 1, 1)
    rows = [
        {"ticker": "AAPL", "date": start + timedelta(days=day), "volume": 1_000 + day}
        for day in range(30)
    ]
    rows.append({"ticker": "AAPL", "date": start + timedelta(days=30), "volume": 10_000})

    anomalies = _volume_spike_anomalies(pd.DataFrame(rows), sigma_threshold=4.0)

    assert len(anomalies) == 1
    assert anomalies[0]["ticker"] == "AAPL"
    assert anomalies[0]["volume"] == 10_000


def test_price_gap_anomaly_fires_on_synthetic_gap() -> None:
    start = date(2026, 1, 1)
    rows = [
        {"ticker": "MSFT", "date": start + timedelta(days=day), "close": 100.0 + (day * 0.1)}
        for day in range(30)
    ]
    rows.append({"ticker": "MSFT", "date": start + timedelta(days=30), "close": 180.0})

    anomalies = _price_gap_anomalies(pd.DataFrame(rows), sigma_threshold=4.0)

    assert len(anomalies) == 1
    assert anomalies[0]["ticker"] == "MSFT"
    assert cast(float, anomalies[0]["gap_pct"]) > 70
