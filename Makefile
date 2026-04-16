.PHONY: setup dev-dagster dev-api dev-frontend test test-integration lint format migrate seed tunnel issue pr build check-prod rollback monitor-install monitor-log help

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
	DAGSTER_HOME=$(CURDIR)/.dagster uv run dagster dev -m dagster_pipelines.definitions -p 3000

dev-api: ## Start FastAPI on localhost:8000
	uv run uvicorn api.main:app --reload --port 8000

dev-frontend: ## Start Next.js on localhost:3001
	cd frontend && npm run dev -- --port 3001

tunnel: ## Open SSH tunnel to Hetzner: ClickHouse (8123) + prod Dagster UI on :3100
	@echo "Opening SSH tunnel:"
	@echo "  localhost:8123 → ClickHouse"
	@echo "  localhost:3100 → prod Dagster UI (local dev Dagster stays on :3000)"
	ssh -N -L 8123:localhost:8123 -L 3100:localhost:3000 hetzner

# ─── Docker ───────────────────────────────────────────────────

check-prod: ## Check prod service status and API health via SSH
	@echo "=== Services ==="
	@ssh hetzner "cd /opt/equity-data-agent && docker compose --profile prod ps"
	@echo ""
	@echo "=== API Health ==="
	@ssh hetzner "curl -sf http://localhost:8000/health && echo '' || echo 'UNREACHABLE'"

rollback: ## Rollback prod to previous commit and rebuild
	@echo "=== Current prod commit ==="
	@ssh hetzner "cd /opt/equity-data-agent && git log --oneline -1"
	@echo ""
	@echo "=== Rolling back to previous commit ==="
	@ssh hetzner "cd /opt/equity-data-agent && git checkout HEAD~1 && docker compose --profile prod up -d --build"
	@echo ""
	@echo "=== Waiting for API to come up (60s max) ==="
	@ssh hetzner 'for i in $$(seq 1 12); do if curl -sf http://localhost:8000/health; then echo ""; echo "Rollback verified OK"; exit 0; fi; echo "Attempt $$i/12..."; sleep 5; done; echo "Health check failed"; exit 1'
	@echo ""
	@echo "=== Rolled back to ==="
	@ssh hetzner "cd /opt/equity-data-agent && git log --oneline -1"

build: ## Build prod Docker images locally (run when changing Dockerfile, docker-compose.yml, or deps)
	docker compose --profile prod build

monitor-install: ## Install health monitor cron on Hetzner (runs every 15 min)
	ssh hetzner "mkdir -p /opt/equity-data-agent/scripts"
	scp scripts/health-monitor.sh hetzner:/opt/equity-data-agent/scripts/health-monitor.sh
	ssh hetzner "chmod +x /opt/equity-data-agent/scripts/health-monitor.sh"
	ssh hetzner '(crontab -l 2>/dev/null | grep -v health-monitor; echo "*/15 * * * * /opt/equity-data-agent/scripts/health-monitor.sh") | crontab -'
	@echo "Health monitor installed — runs every 15 minutes"

monitor-log: ## Show recent health check failures from prod
	@echo "=== Last heartbeat ==="
	@ssh hetzner "cat /opt/equity-data-agent/health-monitor-heartbeat 2>/dev/null || echo 'No heartbeat yet — monitor may not be installed (run make monitor-install)'"
	@echo ""
	@echo "=== Recent failures (last 20) ==="
	@ssh hetzner "tail -20 /opt/equity-data-agent/health-monitor.log 2>/dev/null || echo 'No failures logged'"

# ─── Quality ──────────────────────────────────────────────────

test: ## Run unit tests (no infrastructure required)
	uv run pytest -m "not integration" || [ $$? -eq 5 ]

test-integration: ## Run integration tests (requires: make tunnel)
	uv run pytest -m integration -v

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
