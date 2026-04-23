.PHONY: setup dev-dagster dev-api dev-frontend dev-litellm test test-integration lint format migrate seed tunnel issue pr build check-prod rollback monitor-install monitor-log events-notify-install events-notify-status events-notify-test sops-edit sops-encrypt sops-decrypt sops-rotate-keys help

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

dev-litellm: ## Start LiteLLM proxy on localhost:4000 (reads GROQ_API_KEY / GEMINI_API_KEY from .env)
	# --platform linux/amd64: LiteLLM stable images are amd64-only; runs via Rosetta/QEMU
	# on Apple Silicon (dev-only; prod Hetzner CX41 is native amd64).
	docker run --rm --name equity-litellm-dev --platform linux/amd64 \
		-p 4000:4000 --env-file .env \
		-v $(CURDIR)/litellm_config.yaml:/app/config.yaml \
		litellm/litellm:v1.81.14-stable --config /app/config.yaml --port 4000

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

events-notify-install: ## Install docker-events -> Discord webhook notifier as a systemd service on Hetzner
	ssh hetzner "mkdir -p /opt/equity-data-agent/scripts"
	scp scripts/docker-events-notify.sh hetzner:/opt/equity-data-agent/scripts/docker-events-notify.sh
	scp scripts/docker-events-notify.service hetzner:/etc/systemd/system/docker-events-notify.service
	ssh hetzner "chmod +x /opt/equity-data-agent/scripts/docker-events-notify.sh"
	ssh hetzner "systemctl daemon-reload && systemctl enable --now docker-events-notify.service && systemctl restart docker-events-notify.service"
	@echo ""
	@echo "Installed and restarted. Expect a '[START] docker-events-notify' message in Discord within ~10s."
	@echo "Status: make events-notify-status"

events-notify-status: ## Show status of docker-events-notify service + heartbeat age
	@echo "=== systemd status ==="
	@ssh hetzner "systemctl status docker-events-notify --no-pager" || true
	@echo ""
	@echo "=== Last heartbeat (UTC) ==="
	@ssh hetzner "cat /opt/equity-data-agent/events-notify-heartbeat 2>/dev/null || echo 'no heartbeat — service may be failing'"

events-notify-test: ## Kill litellm to fire a Discord notification (then bring it back manually)
	@echo "Killing equity-data-agent-litellm-1 — expect a Discord [KILL]/[DIE] alert within 30s."
	@ssh hetzner "docker kill equity-data-agent-litellm-1"
	@echo "Discord alert should land. The container will NOT auto-recover — Docker treats"
	@echo "docker kill as 'manually stopped' and skips the restart: unless-stopped policy."
	@echo "Restart with: ssh hetzner 'cd /opt/equity-data-agent && docker compose --profile prod up -d litellm'"

# ─── Secrets (SOPS) ───────────────────────────────────────────

# sops infers file format from extension; `.env.sops` ends in `.sops` (unknown to
# sops, defaults to JSON parser and chokes on `#` comments). Pass explicit
# --input-type/--output-type flags everywhere so the filename stays semantic.

sops-edit: ## Edit .env.sops in-place ($EDITOR opens decrypted; saved content re-encrypted)
	@command -v sops >/dev/null 2>&1 || { echo "sops not installed — brew install sops age"; exit 1; }
	sops --input-type dotenv --output-type dotenv .env.sops

sops-encrypt: ## Encrypt a fresh .env → .env.sops (first-time bootstrap only)
	@command -v sops >/dev/null 2>&1 || { echo "sops not installed — brew install sops age"; exit 1; }
	@[ -f .env ] || { echo ".env not found — create it first with plaintext values"; exit 1; }
	@[ ! -f .env.sops ] || { echo ".env.sops already exists — use 'make sops-edit' instead"; exit 1; }
	sops -e --input-type dotenv --output-type dotenv .env > .env.sops
	@echo "Encrypted .env → .env.sops. Commit .env.sops (keep .env gitignored)."

sops-decrypt: ## Decrypt .env.sops to stdout (read-only; for inspection or round-trip checks)
	@command -v sops >/dev/null 2>&1 || { echo "sops not installed — brew install sops age"; exit 1; }
	@sops -d --input-type dotenv --output-type dotenv .env.sops

sops-rotate-keys: ## Re-encrypt .env.sops under current .sops.yaml recipients (after rotating the age key)
	@command -v sops >/dev/null 2>&1 || { echo "sops not installed — brew install sops age"; exit 1; }
	sops updatekeys .env.sops
	@echo "Re-encrypted .env.sops under the current .sops.yaml recipients."

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
