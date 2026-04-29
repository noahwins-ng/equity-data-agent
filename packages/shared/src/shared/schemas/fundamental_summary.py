from datetime import date, datetime

from pydantic import BaseModel


class FundamentalSummaryRow(BaseModel):
    """Maps to equity_derived.fundamental_summary."""

    ticker: str
    period_end: date
    period_type: str  # 'quarterly' | 'annual' | 'ttm'
    # Valuation
    pe_ratio: float | None = None
    ev_ebitda: float | None = None
    price_to_book: float | None = None
    price_to_sales: float | None = None
    eps: float | None = None
    # Growth
    revenue_yoy_pct: float | None = None
    net_income_yoy_pct: float | None = None
    fcf_yoy_pct: float | None = None
    # Margin deltas (basis points; 100 bps = 1 percentage point)
    gross_margin_bps_yoy: float | None = None
    net_margin_bps_yoy: float | None = None
    # Profitability
    net_margin_pct: float | None = None
    gross_margin_pct: float | None = None
    ebitda_margin_pct: float | None = None
    roe: float | None = None
    roa: float | None = None
    # Cash
    fcf_yield: float | None = None
    # Leverage
    debt_to_equity: float | None = None
    # Liquidity
    current_ratio: float | None = None
    # TTM rollups (only populated on period_type='ttm' rows)
    revenue_ttm: float | None = None
    net_income_ttm: float | None = None
    fcf_ttm: float | None = None
    computed_at: datetime | None = None
