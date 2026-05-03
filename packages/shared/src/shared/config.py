import json
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # Populated by the deploy pipeline via ``docker run --env GIT_SHA=$(git rev-parse HEAD)``;
    # surfaces as the Sentry release tag and as ``deploy.git_sha`` in /health. Empty in
    # local dev runs that don't bother propagating the env var.
    GIT_SHA: str = ""
    # One-shot prod toggle for ``/api/v1/_debug/sentry``. Default off so a scraper
    # looping on the path can't burn the Sentry monthly quota; flip to True for a
    # single deploy when verifying Sentry wiring end-to-end, then revert.
    ENABLE_SENTRY_TEST: bool = False

    # Finnhub /company-news (ADR-015): per-ticker headlines + publisher + image.
    # Free tier — register at https://finnhub.io/register. Empty in dev when news
    # ingest is offline; news_raw asset surfaces a clear error on first call.
    FINNHUB_API_KEY: str = ""

    # Provenance strip values surfaced by /api/v1/health (QNT-132).
    # Single source of truth for the data-driven UI bottom strip — vendor swap
    # or schedule shift updates the API, frontend re-renders without a deploy.
    PROVENANCE_SOURCES: Annotated[list[str], NoDecode] = [
        "yfinance",
        "Finnhub",
        "Qdrant",
    ]
    # Static fallback when Dagster schedule introspection fails (e.g. a future
    # api-only image without dagster_pipelines installed). Format mirrors what
    # the introspected path emits so the frontend never sees a shape change.
    PROVENANCE_NEXT_INGEST_FALLBACK: str = "17:00 ET"

    # ─── QNT-161: public-chat abuse controls ──────────────────────────────
    #
    # CORS allowlist for the public chat panel. Default is dev-only — prod
    # overrides via env. Set CORS_ALLOWED_ORIGINS=https://your-app.vercel.app,
    # http://localhost:3001 to add the deployed frontend without a code change.
    # An origin regex is supported for the Vercel preview-domain pattern; pin
    # it to ONE project (the substring before .vercel.app) so leaked previews
    # for unrelated projects can't drive traffic to this API.
    CORS_ALLOWED_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:3001"]
    # Project-pinned Vercel preview regex. Empty string = no preview origins.
    # Example for project "equity-data-agent":
    #   ^https://equity-data-agent(-[a-z0-9-]+)?\.vercel\.app$
    CORS_ALLOWED_ORIGIN_REGEX: str = ""

    # Per-IP request rate limits applied to POST /api/v1/agent/chat. SlowAPI
    # accepts the semicolon-delimited "5/minute;30/hour;100/day" syntax;
    # exceeding any tier returns 429 with Retry-After. Sized for one
    # recruiter session (the portfolio audience the panel exists to serve).
    CHAT_RATE_LIMIT: str = "5/minute;30/hour;100/day"

    # Per-IP daily Groq token budget. Soft cap orthogonal to request count —
    # a chatty user can stay under 100 requests/day yet exhaust a model TPD
    # by triggering many tool runs. Default ~10K is comfortably more than
    # any single recruiter session (10–15 thesis runs) and well below abuse
    # thresholds. UTC-midnight reset matches Groq's TPD window.
    CHAT_TOKENS_PER_IP_PER_DAY: int = 10_000

    # Global daily Groq token budget — the sum across all IPs. Sized at
    # ~50% of the Llama-3.3-70B free-tier 100K TPD ceiling so daily ingest
    # + the user's own dev usage retain headroom. Once exceeded, every
    # request gets the friendly demo-limit redirect for the rest of the
    # day (FAIL CLOSED — the LiteLLM config has no paid-provider fallback,
    # see ADR-017 / litellm_config.yaml).
    CHAT_TOKENS_GLOBAL_PER_DAY: int = 50_000

    # Burst-alert threshold: if a single IP receives N 429s within
    # CHAT_BURST_WINDOW_SECONDS, fire a Sentry capture_message. Defaults are
    # tuned so a frustrated recruiter retrying twice doesn't trip the alert
    # but a scraper does. Sentry init is gated on SENTRY_DSN (QNT-86 will
    # complete the wiring); without DSN, the alert logs at WARNING.
    CHAT_BURST_THRESHOLD: int = 20
    CHAT_BURST_WINDOW_SECONDS: int = 300

    @property
    def is_prod(self) -> bool:
        return self.ENV == "prod"

    @property
    def clickhouse_url(self) -> str:
        return f"http://{self.CLICKHOUSE_HOST}:{self.CLICKHOUSE_PORT}"

    # pydantic-settings v2 only parses list[str] env values as JSON by default
    # (e.g. CORS_ALLOWED_ORIGINS=["a","b"]). The .env.example comments and the
    # deploy guide document the friendlier comma-separated form
    # (CORS_ALLOWED_ORIGINS=a,b) which crashes EnvSettingsSource without a
    # parser. The 2026-05-02 outage hit prod because of this mismatch — this
    # validator accepts both forms so the documented format actually works.
    @field_validator("CORS_ALLOWED_ORIGINS", "PROVENANCE_SOURCES", mode="before")
    @classmethod
    def _split_comma_list(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if stripped.startswith("["):
            return json.loads(stripped)
        return [item.strip() for item in stripped.split(",") if item.strip()]


settings = Settings()
