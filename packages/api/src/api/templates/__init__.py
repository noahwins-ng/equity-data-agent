"""Report templates for LLM consumption.

Each module exposes ``build_<name>_report(ticker: str) -> str`` which returns a
structured, human-readable text report. All math lives in Dagster/SQL — these
functions only format pre-computed values.
"""

from api.templates.company import build_company_report
from api.templates.fundamental import build_fundamental_report
from api.templates.news import build_news_report
from api.templates.summary import build_summary_report
from api.templates.technical import build_technical_report

__all__ = [
    "build_company_report",
    "build_fundamental_report",
    "build_news_report",
    "build_summary_report",
    "build_technical_report",
]
