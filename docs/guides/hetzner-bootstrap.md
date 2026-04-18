# Hetzner CX41 — Production Bootstrap Guide

One-time setup for a fresh server. After this, every push to `main` auto-deploys via GitHub Actions.

**Prerequisites:** QNT-36 (Docker Compose) and QNT-37 (DDL migrations) merged to main.

> **Domain/Caddy is a Phase 6 concern.** Caddy + DNS is only needed so Vercel (HTTPS) can call the FastAPI backend without mixed-content errors. Until the frontend exists, skip steps 5 and 6 and access the API directly on port 8000.

---

## 1. Provision the Server

In the [Hetzner Cloud dashboard](https://console.hetzner.cloud):

- Type: **CX41** (4 vCPU, 16 GB RAM, 160 GB disk)
- OS: **Ubuntu 22.04**
- SSH key: add your public key (`~/.ssh/id_ed25519.pub` or similar)
- Note the **public IP address**

---

## 2. Install Docker and Make (first SSH into the server)

SSH into the server and install Docker Engine and make:

```bash
ssh root@<your-ip>
curl -fsSL https://get.docker.com | sh
apt install make -y
```

Verify:

```bash
docker --version
docker compose version
make --version
```

---

## 3. Clone the Repo

```bash
git clone https://github.com/noahwins-ng/equity-data-agent.git /opt/equity-data-agent
cd /opt/equity-data-agent
```

---

## 4. Configure Environment

```bash
cp .env.example .env
nano .env
```

Set all production values:

| Variable | Value |
|---|---|
| `ENV` | `prod` |
| `CLICKHOUSE_HOST` | `clickhouse` (Docker service name — not localhost) |
| `CLICKHOUSE_PORT` | `8123` |
| `LITELLM_BASE_URL` | `http://litellm:4000` (Docker service name — not localhost) |
| `QDRANT_URL` | Your Qdrant Cloud cluster URL |
| `QDRANT_API_KEY` | From Qdrant Cloud dashboard |
| `OLLAMA_API_KEY` | From [ollama.com](https://ollama.com) account |
| `ANTHROPIC_API_KEY` | Optional — Claude API override |
| `LANGFUSE_PUBLIC_KEY` | From Langfuse dashboard |
| `LANGFUSE_SECRET_KEY` | From Langfuse dashboard |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` |
| `SENTRY_DSN` | From Sentry project settings |
| `NEXT_PUBLIC_API_URL` | `https://your-domain.com` |

---

## 5. Expose Port 8000 (no-domain setup)

Port 8000 is already exposed in `docker-compose.yml` — no manual edit needed. This allows direct access via `http://<your-ip>:8000` until a domain + Caddy is added in Phase 6.

The `caddy` service will fail to start without a valid domain — that's expected and won't affect the rest of the stack.

> **Phase 6:** Remove the `ports` exposure from the `api` service, update `Caddyfile` with your real domain, point DNS, and Caddy handles HTTPS automatically.

---

## 6. Add GitHub Secrets

In GitHub: repo **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|---|---|
| `HETZNER_HOST` | Server public IP |
| `HETZNER_USER` | `root` |
| `HETZNER_SSH_KEY` | Contents of your private key (including `-----BEGIN` / `-----END` lines) |

To get your private key:
```bash
cat ~/.ssh/id_ed25519   # or whichever key you added to the server
```

---

## 7. First Deploy

On the server:

```bash
cd /opt/equity-data-agent
docker compose --profile prod up -d --build
```

This will pull images and build the `dagster` and `api` containers. Takes a few minutes on first run.

---

## 8. Run DDL Migrations

Wait for ClickHouse to be healthy, then run migrations:

```bash
# Check ClickHouse is up
docker compose exec clickhouse clickhouse-client --query "SELECT 1"

# Run migrations
make migrate
```

---

## 9. Verify

```bash
# All services running
docker compose ps

# API health check (no domain — direct IP)
curl http://<your-ip>:8000/health
```

Expected: all services `Up`, health check returns `200 OK`.

**Acceptance criteria (from QNT-83):**
- [ ] All prod services healthy: `clickhouse`, `dagster`, `dagster-daemon`, `api`, `litellm`
- [ ] `http://<your-ip>:8000/health` returns 200
- [ ] GitHub Actions CD completes on next push to main
- [ ] ClickHouse databases `equity_raw` and `equity_derived` with all 9 tables exist
- [ ] GitHub secrets configured

> `caddy` is deferred to Phase 6 when a domain is available.

---

## 10. Unattended-Upgrades Mail Alerts

`unattended-upgrades` silently schedules a host reboot when a kernel/libc update requires one. On 2026-04-18 this went unnoticed for ~21 hours — see the QNT-95/QNT-96 incident — so we require mail delivery on every upgrade run.

**Install a mail transport** (once):

```bash
apt install -y bsd-mailx
# `bsd-mailx` pulls in postfix with sensible defaults (local smarthost).
# Alternative: `mailutils` + `msmtp` if you need SMTP relay to Gmail/SES.
```

**Edit** `/etc/apt/apt.conf.d/50unattended-upgrades` to uncomment + set:

```
Unattended-Upgrade::Mail "noahwins.dev@gmail.com";
Unattended-Upgrade::MailReport "on-change";
```

`on-change` emits a mail only when packages were actually upgraded (or an error occurred) — not on every no-op run.

**Verify mail actually sends**:

```bash
echo "test from $(hostname) $(date -u +%FT%TZ)" | mail -s "hetzner mail test" noahwins.dev@gmail.com
# Then check the inbox. If nothing arrives, inspect /var/log/mail.log.
```

Mail that never leaves the box is worse than no alerting — don't skip this check.

**Pending-reboot surfacing.** `scripts/health-monitor.sh` also logs a `REBOOT REQUIRED` line whenever `/var/run/reboot-required` exists (every 15 min via cron), so `make monitor-log` and the Claude Code session-start hook both surface it even if mail delivery is broken.

---

## Subsequent Deploys

After the bootstrap, every merge to `main` triggers automatic deployment:

```
push to main → GitHub Actions → SSH into server → git pull → docker compose --profile prod up -d --build
```

You never need to SSH in for routine deploys.

## Accessing Internal Services

Services not exposed publicly are accessible via SSH tunnel:

```bash
# Dagster UI
ssh -L 3000:localhost:3000 root@<your-ip>
# then open http://localhost:3000

# ClickHouse Play UI
ssh -L 8123:localhost:8123 root@<your-ip>
# then open http://localhost:8123/play
```
