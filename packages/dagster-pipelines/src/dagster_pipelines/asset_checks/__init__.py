from dagster_pipelines.asset_checks.fundamental_summary_checks import (
    fundamental_summary_net_margin_in_band,
    fundamental_summary_no_infinities,
    fundamental_summary_pe_in_band,
)
from dagster_pipelines.asset_checks.fundamentals_checks import (
    fundamentals_has_rows,
    fundamentals_period_type_valid,
    fundamentals_revenue_and_net_income_populated,
)
from dagster_pipelines.asset_checks.ohlcv_raw_checks import (
    ohlcv_raw_dates_fresh,
    ohlcv_raw_has_rows,
    ohlcv_raw_no_future_dates,
    ohlcv_raw_no_null_close,
)
from dagster_pipelines.asset_checks.technical_indicators_checks import (
    daily_macd_signal_coherent,
    daily_recent_no_nan,
    daily_rsi_in_range,
    monthly_rsi_in_range,
    weekly_macd_signal_coherent,
    weekly_recent_no_nan,
    weekly_rsi_in_range,
)

ALL_ASSET_CHECKS = [
    # ohlcv_raw
    ohlcv_raw_has_rows,
    ohlcv_raw_no_null_close,
    ohlcv_raw_no_future_dates,
    ohlcv_raw_dates_fresh,
    # fundamentals
    fundamentals_has_rows,
    fundamentals_period_type_valid,
    fundamentals_revenue_and_net_income_populated,
    # technical_indicators
    daily_rsi_in_range,
    daily_macd_signal_coherent,
    daily_recent_no_nan,
    weekly_rsi_in_range,
    weekly_macd_signal_coherent,
    weekly_recent_no_nan,
    monthly_rsi_in_range,
    # fundamental_summary
    fundamental_summary_pe_in_band,
    fundamental_summary_net_margin_in_band,
    fundamental_summary_no_infinities,
]

__all__ = [
    "ALL_ASSET_CHECKS",
    "daily_macd_signal_coherent",
    "daily_recent_no_nan",
    "daily_rsi_in_range",
    "fundamental_summary_net_margin_in_band",
    "fundamental_summary_no_infinities",
    "fundamental_summary_pe_in_band",
    "fundamentals_has_rows",
    "fundamentals_period_type_valid",
    "fundamentals_revenue_and_net_income_populated",
    "monthly_rsi_in_range",
    "ohlcv_raw_dates_fresh",
    "ohlcv_raw_has_rows",
    "ohlcv_raw_no_future_dates",
    "ohlcv_raw_no_null_close",
    "weekly_macd_signal_coherent",
    "weekly_recent_no_nan",
    "weekly_rsi_in_range",
]
