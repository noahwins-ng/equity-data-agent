.PHONY: setup dev-dagster dev-api dev-frontend test lint format migrate seed tunnel issue pr build help

# ─── Setup ────────────────────────────────────────────────────

setup: ## First-time repo setup: hooks, deps, env
	git config core.hooksPath .githooks
	uv sync
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "Created .env from .env.example — edit it with your values"; \
	fi
	@echo "Setup complete."

# ─── Development ──────────────────────────────────────────────

dev-dagster: ## Start Dagster UI on localhost:3000
	uv run dagster dev -m dagster_pipelines.definitions -p 3000

dev-api: ## Start FastAPI on localhost:8000
	uv run uvicorn api.main:app --reload --port 8000

dev-frontend: ## Start Next.js on localhost:3001
	cd frontend && npm run dev -- --port 3001

tunnel: ## Open SSH tunnel to Hetzner ClickHouse (port 8123)
	@echo "Opening SSH tunnel: localhost:8123 → Hetzner ClickHouse"
	ssh -N -L 8123:localhost:8123 hetzner

# ─── Docker ───────────────────────────────────────────────────

build: ## Build prod Docker images locally (run when changing Dockerfile, docker-compose.yml, or deps)
	docker compose --profile prod build

# ─── Quality ──────────────────────────────────────────────────

test: ## Run all tests
	uv run pytest

lint: ## Run linter + type checker
	uv run ruff check .
	uv run pyright

format: ## Auto-format code
	uv run ruff format .
	uv run ruff check --fix .

# ─── Database ─────────────────────────────────────────────────

migrate: ## Run ClickHouse DDL migrations via HTTP interface
	@for f in migrations/*.sql; do \
		echo "Running $$f..."; \
		curl -s --fail \
			"http://$${CLICKHOUSE_HOST:-localhost}:$${CLICKHOUSE_PORT:-8123}/" \
			--data-binary @"$$f" \
			&& echo " OK" \
			|| echo " FAILED"; \
	done

seed: ## Quick seed: 30 days of data for 3 tickers (fast dev setup)
	uv run python -m dagster_pipelines.seed

# ─── Git Workflow ─────────────────────────────────────────────

issue: ## Checkout branch for a Linear issue (usage: make issue QNT=34)
ifndef QNT
	$(error Usage: make issue QNT=34)
endif
	@git checkout -b "noahwinsdev/qnt-$(QNT)" 2>/dev/null || git checkout "noahwinsdev/qnt-$(QNT)"
	@echo "On branch noahwinsdev/qnt-$(QNT)"
	@echo "Tip: rename with 'git branch -m noahwinsdev/qnt-$(QNT)-your-description'"

pr: ## Create PR and push (usage: make pr QNT=34 TITLE="your title")
ifndef QNT
	$(error Usage: make pr QNT=34 TITLE="your title")
endif
ifndef TITLE
	$(error Usage: make pr QNT=34 TITLE="your title")
endif
	git push -u origin HEAD
	gh pr create \
		--title "QNT-$(QNT): $(TITLE)" \
		--body "$$(cat <<'EOF'\nCloses QNT-$(QNT)\n\n---\n\nGenerated with [Claude Code](https://claude.com/claude-code)\nEOF\n)"

# ─── Help ─────────────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
