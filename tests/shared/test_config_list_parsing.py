"""Regression test for the 2026-05-02 prod outage.

`CORS_ALLOWED_ORIGINS` is a `list[str]` field. Pydantic-settings v2 only parses
list[str] env values as JSON by default (`["a","b"]`), but `.env.example` and
the deploy guide document the friendlier comma-separated form (`a,b`). The
mismatch crashed prod when the comma-separated value landed in `.env.sops`.

A `field_validator(mode="before")` on the field accepts both formats. These
tests pin that contract — a future config refactor that drops the validator
will fail here, not in prod.
"""

from __future__ import annotations

import pytest
from shared.config import Settings


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip env vars that would otherwise leak from the dev shell."""
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("PROVENANCE_SOURCES", raising=False)


@pytest.mark.usefixtures("base_env")
def test_cors_origins_default() -> None:
    """No env override → ships with the dev-only default."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.CORS_ALLOWED_ORIGINS == ["http://localhost:3001"]


@pytest.mark.usefixtures("base_env")
def test_cors_origins_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The format documented in .env.example must parse."""
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "https://example.vercel.app,http://localhost:3001",
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.CORS_ALLOWED_ORIGINS == [
        "https://example.vercel.app",
        "http://localhost:3001",
    ]


@pytest.mark.usefixtures("base_env")
def test_cors_origins_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pydantic-settings native format must keep working."""
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        '["https://example.vercel.app","http://localhost:3001"]',
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.CORS_ALLOWED_ORIGINS == [
        "https://example.vercel.app",
        "http://localhost:3001",
    ]


@pytest.mark.usefixtures("base_env")
def test_cors_origins_comma_with_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trailing/leading whitespace per item is trimmed; empty items dropped."""
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "  https://example.vercel.app , http://localhost:3001 , ",
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.CORS_ALLOWED_ORIGINS == [
        "https://example.vercel.app",
        "http://localhost:3001",
    ]


@pytest.mark.usefixtures("base_env")
def test_provenance_sources_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same parser applies to PROVENANCE_SOURCES (the other list[str] field)."""
    monkeypatch.setenv("PROVENANCE_SOURCES", "yfinance,Finnhub,Qdrant")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.PROVENANCE_SOURCES == ["yfinance", "Finnhub", "Qdrant"]
