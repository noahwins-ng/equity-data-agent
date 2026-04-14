from datetime import date, datetime

from pydantic import BaseModel


class _TechnicalIndicatorsBase(BaseModel):
    ticker: str
    sma_20: float | None = None
    sma_50: float | None = None
    ema_12: float | None = None
    ema_26: float | None = None
    rsi_14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    computed_at: datetime | None = None


class TechnicalIndicatorsDailyRow(_TechnicalIndicatorsBase):
    """Maps to equity_derived.technical_indicators_daily."""

    date: date


class TechnicalIndicatorsWeeklyRow(_TechnicalIndicatorsBase):
    """Maps to equity_derived.technical_indicators_weekly."""

    week_start: date


class TechnicalIndicatorsMonthlyRow(_TechnicalIndicatorsBase):
    """Maps to equity_derived.technical_indicators_monthly."""

    month_start: date
