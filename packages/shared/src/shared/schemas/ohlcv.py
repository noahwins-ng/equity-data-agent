from datetime import date, datetime

from pydantic import BaseModel


class OHLCVRow(BaseModel):
    """Maps to equity_raw.ohlcv_raw."""

    ticker: str
    date: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: int
    fetched_at: datetime | None = None


class OHLCVWeeklyRow(BaseModel):
    """Maps to equity_derived.ohlcv_weekly."""

    ticker: str
    week_start: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: int
    computed_at: datetime | None = None


class OHLCVMonthlyRow(BaseModel):
    """Maps to equity_derived.ohlcv_monthly."""

    ticker: str
    month_start: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: int
    computed_at: datetime | None = None
