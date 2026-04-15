# Retrospective: Phase 0 — Foundation

**Timeline:** Apr 12 → Apr 15, 2026 (1 cycle)
**Shipped:** 7 issues, 17 PRs merged

---

## Issues Closed

| Issue | Title | PRs |
|---|---|---|
| QNT-34 | Monorepo init (uv workspaces) | 1 |
| QNT-35 | Shared package (config, tickers, schemas) | 1 |
| QNT-36 | Docker Compose dev/prod profiles | 1 |
| QNT-37 | ClickHouse DDL migrations | 2 (multi-statement fix) |
| QNT-38 | GitHub Actions CI + CD | 2 (pytest exit code fix) |
| QNT-39 | CLAUDE.md, Makefile, env config | 1 |
| QNT-83 | Hetzner production bootstrap | 9 |

---

## What Went Well

- Monorepo + shared package (QNT-34, QNT-35) were clean — no rework
- GitHub Actions CI/CD structure was solid from the start
- DDL schema design held up — no changes needed after first pass

---

## What Was Harder Than Expected

**QNT-83 took 9 PRs** due to cascading infra failures:

1. LiteLLM image not found on GHCR → switched to Docker Hub (`litellm/litellm:v1.56.5`)
2. `uv sync` without `--all-packages` → empty venv, all services fail at import
3. `uv run` in docker-compose commands → re-syncs env on every container start
4. `DAGSTER_HOME` not set → dagster-daemon crash
5. Hetzner firewall blocking GitHub Actions SSH (personal IP only)

**ClickHouse HTTP rejects multi-statement DDL** — required splitting `000_create_databases.sql` into two files.

**No domain available** → Caddy deferred to Phase 6; API exposed directly on `:8000`.

---

## Lessons Learned

| Area | Lesson |
|---|---|
| Docker + uv | Always use `uv sync --frozen --no-dev --all-packages` in Dockerfile |
| Docker + uv | Use full venv paths in compose commands (`/app/.venv/bin/...`), not `uv run` |
| ClickHouse | One SQL statement per migration file — HTTP interface rejects multi-statement |
| Hetzner | Firewall port 22 must allow `0.0.0.0/0` for GitHub Actions CD |
| Dagster | `DAGSTER_HOME=/dagster_home` env var + `mkdir -p /dagster_home` in Dockerfile required |
| LiteLLM | Image is `litellm/litellm` on Docker Hub, not `ghcr.io/berriai/litellm` |

---

## Next: Phase 1 — Data Ingestion

| Issue | Title | Priority |
|---|---|---|
| QNT-41 | `ohlcv_raw` asset (yfinance → ClickHouse) | High |
| QNT-42 | `fundamentals` asset (yfinance → ClickHouse) | High |
| QNT-43 | Dagster schedules (daily OHLCV, weekly fundamentals) | Medium |
| QNT-82 | `make seed` (30d × 3 tickers for local dev) | Low |

**Risk flags:**
- `dagster_pipelines/__init__.py` is empty — `definitions.py` + ClickHouse resource need to be set up before any asset can materialise
- yfinance rate limits are unpredictable; exponential backoff is essential
- 2-year backfill × 10 tickers will be the first real load test on Hetzner ClickHouse
