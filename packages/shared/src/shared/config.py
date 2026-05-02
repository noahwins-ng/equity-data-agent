from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ENV: str = "dev"

    # ClickHouse
    CLICKHOUSE_HOST: str = "localhost"
    CLICKHOUSE_PORT: int = 8123

    # Qdrant Cloud
    QDRANT_URL: str = ""
    QDRANT_API_KEY: str = ""

    # Agent → FastAPI (QNT-57 tool wrappers). Dev: localhost:8000; when the
    # agent is containerized in Phase 5, override with http://api:8000.
    API_BASE_URL: str = "http://localhost:8000"

    # LiteLLM proxy (see ADR-011 — Groq default, Gemini 2.5 Flash override)
    LITELLM_BASE_URL: str = "http://localhost:4000"
    GROQ_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    EQUITY_AGENT_PROVIDER: str = "groq"  # "groq" | "gemini"
    # Per-LLM-call request timeout in seconds. Bound for any single
    # ChatOpenAI request (plan / synthesize / structured-output) so a hung
    # LiteLLM proxy or stalled provider can never stall the SSE chat
    # connection forever (QNT-150). Free-tier providers (Groq, Gemini) take
    # under 10s on the happy path; 60s is loose enough to absorb the slow
    # tail without leaving SSE clients hanging indefinitely.
    LLM_REQUEST_TIMEOUT: float = 60.0
    # Top-level budget for an entire chat-SSE run. asyncio.wait_for around
    # the graph runner enforces this regardless of internal LLM-level
    # timeouts (a misbehaving proxy could retry past the per-call cap).
    # Default = 4× per-call timeout, matching the worst-case classify +
    # plan + synthesize + safety margin.
    CHAT_RUN_TIMEOUT: float = 240.0

    # Langfuse
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_BASE_URL: str = "https://us.cloud.langfuse.com"

    # Sentry
    SENTRY_DSN: str = ""

    # Finnhub /company-news (ADR-015): per-ticker headlines + publisher + image.
    # Free tier — register at https://finnhub.io/register. Empty in dev when news
    # ingest is offline; news_raw asset surfaces a clear error on first call.
    FINNHUB_API_KEY: str = ""

    # Provenance strip values surfaced by /api/v1/health (QNT-132).
    # Single source of truth for the data-driven UI bottom strip — vendor swap
    # or schedule shift updates the API, frontend re-renders without a deploy.
    PROVENANCE_SOURCES: list[str] = ["yfinance", "Finnhub", "Qdrant"]
    # Static fallback when Dagster schedule introspection fails (e.g. a future
    # api-only image without dagster_pipelines installed). Format mirrors what
    # the introspected path emits so the frontend never sees a shape change.
    PROVENANCE_NEXT_INGEST_FALLBACK: str = "17:00 ET"

    @property
    def is_prod(self) -> bool:
        return self.ENV == "prod"

    @property
    def clickhouse_url(self) -> str:
        return f"http://{self.CLICKHOUSE_HOST}:{self.CLICKHOUSE_PORT}"


settings = Settings()
