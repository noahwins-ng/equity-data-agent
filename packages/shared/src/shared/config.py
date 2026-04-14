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

    # LiteLLM proxy
    LITELLM_BASE_URL: str = "http://localhost:4000"
    OLLAMA_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""

    # Langfuse
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

    # Sentry
    SENTRY_DSN: str = ""

    @property
    def is_prod(self) -> bool:
        return self.ENV == "prod"

    @property
    def clickhouse_url(self) -> str:
        return f"http://{self.CLICKHOUSE_HOST}:{self.CLICKHOUSE_PORT}"


settings = Settings()
