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

    # Langfuse
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_BASE_URL: str = "https://us.cloud.langfuse.com"

    # Sentry
    SENTRY_DSN: str = ""

    @property
    def is_prod(self) -> bool:
        return self.ENV == "prod"

    @property
    def clickhouse_url(self) -> str:
        return f"http://{self.CLICKHOUSE_HOST}:{self.CLICKHOUSE_PORT}"


settings = Settings()
