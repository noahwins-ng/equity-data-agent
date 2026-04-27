from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class NewsRawRow(BaseModel):
    """Maps to equity_raw.news_raw.

    publisher_name / image_url / sentiment_label landed in QNT-141 per ADR-015
    (migrations 012-014). Existing rows from the prior Yahoo RSS path default
    these to '' / '' / 'pending' respectively; new Finnhub rows populate them.
    """

    id: int  # blake2b(url) truncated to UInt64
    ticker: str
    headline: str
    body: str
    source: str  # ingest provenance: "finnhub" (post-QNT-141) | "yahoo_finance" (legacy)
    url: str
    published_at: datetime
    fetched_at: datetime | None = None
    publisher_name: str = ""
    image_url: str = ""
    sentiment_label: Literal["pending", "positive", "neutral", "negative"] = "pending"
