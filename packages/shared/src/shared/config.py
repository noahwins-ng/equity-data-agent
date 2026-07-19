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
    # QNT-381: credentials sent by every app client (Dagster resource, api
    # client, scripts). Prod sets a password on the default user via the
    # clickhouse service environment in docker-compose.yml; the empty-string
    # defaults preserve a passwordless local ClickHouse for tests.
    CLICKHOUSE_USER: str = "default"
    CLICKHOUSE_PASSWORD: str = ""

    # Qdrant Cloud
    QDRANT_URL: str = ""
    QDRANT_API_KEY: str = ""

    # Agent → FastAPI (QNT-57 tool wrappers). Dev: localhost:8000; when the
    # agent is containerized in Phase 5, override with http://api:8000.
    API_BASE_URL: str = "http://localhost:8000"

    # LiteLLM proxy (see ADR-011 — Groq default, Gemini 2.5 Flash override)
    LITELLM_BASE_URL: str = "http://localhost:4000"
    GROQ_API_KEY: str = ""
    CEREBRAS_API_KEY: str = ""
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

    # ─── Alerting (QNT-62 + QNT-101) ─────────────────────────────────────
    # Discord webhook used by docker-events-notify.sh (container die/kill/oom)
    # and dagster_run_failure_alert_sensor (asset materialization failures).
    # Create at: Discord server → Server Settings → Integrations → Webhooks.
    # Empty in dev unless wiring the alert path end-to-end; the sensor logs and
    # skips when unset so test runs / local dev never POST anywhere.
    DISCORD_WEBHOOK_URL: str = ""
    # Base URL of the Dagster webserver, used to build clickable run links in
    # Discord messages. Local dev / prod-via-tunnel both reach the UI at
    # http://localhost:3000 (the prod webserver is not exposed publicly —
    # operator follows the SSH tunnel to view the run).
    DAGSTER_BASE_URL: str = "http://localhost:3000"

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

    # SEC EDGAR 8-K earnings-release ingestion (QNT-260). SEC fair-use requires
    # a declared User-Agent carrying a contact address; there is no API key.
    # https://www.sec.gov/os/webmaster-faq#developers — keep the email current so
    # SEC can reach us before rate-limiting. QNT-388: the repo is public, so the
    # code default is a placeholder — prod MUST set a real contact via env (the
    # field is passed through to Dagster run-workers via dagster.yaml env_vars).
    SEC_EDGAR_USER_AGENT: str = "equity-data-agent contact@example.com"

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

    # ─── QNT-168: Vercel Deploy Hook (SSG freshness driver) ──────────────
    #
    # /ticker/[symbol] and / are statically rendered at build time. After
    # each Dagster ingest cycle (ohlcv weekday EOD, news daily 02:00 ET,
    # fundamentals Sunday) a Dagster schedule POSTs this URL so Vercel
    # rebuilds the frontend with the freshly ingested data. The hook URL
    # itself is the authentication token -- there is no secret to share
    # across two control planes.
    #
    # Empty in dev unless the developer is wiring the webhook end-to-end;
    # the op logs a warning and skips when unset. Generate at:
    #   Vercel project → Settings → Git → Deploy Hooks → Create Hook.
    VERCEL_DEPLOY_HOOK_URL: str = ""

    # ─── QNT-192: Weekly online eval ─────────────────────────────────────────
    #
    # Sample rate for the weekly online eval Dagster schedule. Default 5% —
    # bump to 1.0 if the first month produces < 20 sampled traces / week.
    # Configurable without a deploy: update .env and restart dagster-daemon.
    ONLINE_EVAL_SAMPLE_RATE: float = 0.05
    # Separate Langfuse keys for the online eval schedule. In practice these
    # point at the same Langfuse project as LANGFUSE_PUBLIC_KEY / SECRET_KEY
    # (to read prod traces and push scores back), but isolating them as
    # distinct env vars ensures evals/__main__.py's key-stripping pattern
    # never silently kills the schedule.
    ONLINE_EVAL_LANGFUSE_PUBLIC_KEY: str = ""
    ONLINE_EVAL_LANGFUSE_SECRET_KEY: str = ""

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
    # accepts the semicolon-delimited "5/minute;20/day" syntax; exceeding any
    # tier returns 429 with Retry-After. The 20/day tier is the advertised
    # per-visitor cap (recruiter portfolio audience); 5/minute is a burst
    # guard so those 20 can't be fired in one scripted spray.
    CHAT_RATE_LIMIT: str = "5/minute;20/day"

    # QNT-388: per-IP request rate limit for GET /api/v1/search/{news,earnings}.
    # Each call bills a Qdrant Cloud Inference embedding (plus an optional
    # Cohere rerank), so unthrottled public access is a cost vector even though
    # no LLM is involved. Generous for a human exploring the API by hand;
    # bounds a scripted scraper. Internal agent-tool calls (loopback / Docker
    # bridge, never transited Cloudflare) are exempt via ``search_rate_key`` —
    # see api/security.py — so concurrent chat runs can't starve each other
    # through a shared internal bucket.
    SEARCH_RATE_LIMIT: str = "30/minute;1000/day"

    # Per-IP daily token budget. Soft cap orthogonal to request count — a
    # chatty user can stay under the request cap yet run up cost by triggering
    # many tool runs. Sized so the 20/day request cap is the one that actually
    # binds: a substantive thesis chat is ~14K tokens (see
    # CHAT_TOKENS_GLOBAL_PER_DAY), so 20 × 14K = 280K keeps the token fence
    # from cutting a visitor short of the advertised 20 chats. UTC-midnight
    # reset. ~$0.04/visitor/day worst case at DeepSeek pricing.
    CHAT_TOKENS_PER_IP_PER_DAY: int = 280_000

    # Global daily token budget — the sum across all IPs. QNT-258 / ADR-025:
    # re-derived for the paid launch primary (DeepSeek V4 Flash via OpenRouter).
    # The old 200K was ~50% of the Groq free-tier TPD and tripped after ~15
    # substantive chats — a silent quota wall on launch night. On a paid plan
    # there is no provider TPD ceiling, so this stops proxying "free tokens
    # left" and becomes a pure runaway-cost / abuse circuit breaker.
    #
    # Sizing (paid economics): a substantive thesis chat is ~14K tokens at
    # ~$0.002 (DeepSeek $0.09/$0.18 per M). The launch envelope is ~$1/day
    # (~500 chats); 20M tokens is ~2.8x that (~1,400 chats) with a worst-case
    # ceiling of ~$2.7/day — comfortably above a good launch evening + daily
    # ingest + dev/eval sweeps, yet still bounding a stuck loop or scraper to a
    # few dollars/day. The tight per-user fences are UNCHANGED and do the real
    # anti-abuse work: per-IP token budget (280K/day) + rate limit (20/day).
    # Still FAIL CLOSED — once exceeded, every request gets the friendly
    # demo-limit redirect until UTC midnight (see api/security.py).
    CHAT_TOKENS_GLOBAL_PER_DAY: int = 20_000_000

    # QNT-388: estimated tokens charged per LLM call whose response carried no
    # usage block (the LiteLLM proxy is known to strip ``usage`` on some
    # structured-output paths). Without this, a run whose calls ALL lost their
    # usage would debit zero and the budgets above would silently never
    # advance — the cost breaker would be fiction. A substantive thesis chat
    # is ~14K tokens across ~4 LLM calls (~3.5K/call); 5K over-charges on
    # purpose — a budget breaker must fail toward tripping early, not late.
    CHAT_TOKENS_USAGE_FALLBACK_PER_CALL: int = 5_000

    # Burst-alert threshold: if a single IP receives N 429s within
    # CHAT_BURST_WINDOW_SECONDS, fire a Sentry capture_message. Defaults are
    # tuned so a frustrated recruiter retrying twice doesn't trip the alert
    # but a scraper does. Sentry init is gated on SENTRY_DSN (QNT-86 will
    # complete the wiring); without DSN, the alert logs at WARNING.
    CHAT_BURST_THRESHOLD: int = 20
    CHAT_BURST_WINDOW_SECONDS: int = 300

    # ─── QNT-209: Agent session memory (SqliteSaver) ─────────────────────────
    #
    # Path on the api container to the SQLite database holding LangGraph
    # checkpoints + the QNT-209 thread_last_seen sidecar table. The /var/lib/
    # agent prefix matches the named volume mount in docker-compose.yml so the
    # database persists across container restarts (AC2/AC11). Override to a
    # tmp path in tests.
    AGENT_DB_PATH: str = "/var/lib/agent/agent.db"
    # Days to retain a thread's checkpoint history. Refresh creates a new
    # thread per browser session; abandoned sessions get pruned after this
    # window. Tests override to 0 for immediate pruning.
    AGENT_THREAD_TTL_DAYS: int = 7
    # How often the prune loop fires inside the api lifespan task.
    # 86_400 = once per day. Tests override to a few seconds.
    AGENT_THREAD_PRUNE_INTERVAL_SECONDS: int = 86_400

    # ─── QNT-262: hybrid retrieval (dense + BM25 RRF) + Cohere rerank ─────────
    #
    # Hybrid fuses the dense MiniLM ranking with a client-side BM25 ranking over
    # the ticker-scoped corpus via RRF — no Qdrant schema change, no re-index
    # (the collections stay dense-only). Master switch so the dense-only path
    # stays one flag away if fusion ever regresses a query class.
    HYBRID_SEARCH_ENABLED: bool = True
    # Cohere Rerank 3.5 cross-encoder over the fused candidate set. Gated TWICE:
    # the agent only calls search_news on a targeted (needs_news_search) query,
    # AND the rerank no-ops when COHERE_API_KEY is empty — so the hot path is
    # never taxed and a missing key degrades to the fused order, not an error.
    # Free trial: 1000 calls/mo, 10 rpm. Register at https://dashboard.cohere.com.
    COHERE_API_KEY: str = ""
    COHERE_RERANK_MODEL: str = "rerank-v3.5"
    # Fused candidates fed to the reranker. Wider than the returned top-k (4-8)
    # so the cross-encoder can pull a buried-but-relevant hit up into the cut.
    RERANK_CANDIDATES: int = 20

    # ─── QNT-273: contextual retrieval (index-time chunk-context enrichment) ──
    #
    # At index time, an LLM writes a 1-2 sentence blurb situating each 8-K chunk
    # in its parent release; the blurb is prepended to the chunk before embedding
    # (Anthropic Contextual Retrieval, Sep 2024). Enrichment is index-time only;
    # never on the query hot path.
    #
    # Master switch — left OFF after the QNT-261 A/B (run_id 97602ba8, 13 earnings
    # queries / AAPL+NVDA): the lift was mixed — MRR +6.9%, nDCG@10 +4.0%, R@20
    # +4.8%, but R@5 -4.0%. Ranking quality improves (directionally matching
    # Anthropic) but the R@5 dip is within noise on a 2-ticker sample, and a
    # recurring per-chunk ingest LLM call isn't justified on that. DECISION: HOLD
    # — capability stays one flag-flip away; revisit when the earnings golden set
    # grows. ``uv run python -m agent.evals.retrieval_eval --contextual`` re-runs
    # the A/B.
    EARNINGS_CONTEXTUAL: bool = False
    # LiteLLM alias for the enrichment call. The free gpt-oss-20b on Groq — a
    # gpt-oss model, so Groq prompt-caches the repeated parent-doc prefix across
    # a release's ~30 chunks (reference_groq_prompt_caching), making the
    # whole-document context the Anthropic method wants nearly free per chunk.
    CONTEXT_MODEL: str = "equity-agent/small"
    # Parent-document text is truncated to this many chars before the enrichment
    # call. An 8-K leads with its dateline + summary (company, quarter, headline
    # numbers — exactly the situating context a chunk needs), so the first ~4k
    # chars ground the blurb while keeping each call's input under the free-tier
    # per-minute token ceiling; a 12k window measured ~15s/call from Groq TPM
    # throttling vs ~3s at 4k (QNT-273).
    CONTEXT_MAX_DOC_CHARS: int = 4_000
    # Seconds slept between enrichment calls during a contextual ingest run, to
    # stay under the free-tier RPM ceiling. Index-time only, so latency is free.
    CONTEXT_THROTTLE_SECONDS: float = 1.0

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
