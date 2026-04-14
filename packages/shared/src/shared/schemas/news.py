from datetime import datetime

from pydantic import BaseModel


class NewsRawRow(BaseModel):
    """Maps to equity_raw.news_raw."""

    id: int  # sipHash64(concat(ticker, url))
    ticker: str
    headline: str
    body: str
    source: str
    url: str
    published_at: datetime
    fetched_at: datetime | None = None
