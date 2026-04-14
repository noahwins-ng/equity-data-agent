from datetime import date, datetime

from pydantic import BaseModel


class FundamentalsRow(BaseModel):
    """Maps to equity_raw.fundamentals."""

    ticker: str
    period_end: date
    period_type: str  # 'quarterly' | 'annual'
    revenue: float
    gross_profit: float
    net_income: float
    total_assets: float
    total_liabilities: float
    current_assets: float
    current_liabilities: float
    free_cash_flow: float
    ebitda: float
    total_debt: float
    cash_and_equivalents: float
    shares_outstanding: int
    market_cap: float
    fetched_at: datetime | None = None
