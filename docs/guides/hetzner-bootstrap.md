# Hetzner CX41 — Production Bootstrap Guide

One-time setup for a fresh server. After this, every push to `main` auto-deploys via GitHub Actions.

**Prerequisites:** QNT-36 (Docker Compose) and QNT-37 (DDL migrations) merged to main.

---

## 1. Provision the Server

In the [Hetzner Cloud dashboard](https://console.hetzner.cloud):

- Type: **CX41** (4 vCPU, 16 GB RAM, 160 GB disk)
- OS: **Ubuntu 22.04**
- SSH key: add your public key (`~/.ssh/id_ed25519.pub` or similar)
- Note the **public IP address**

---

## 2. Install Docker

SSH into the server and install Docker Engine:

```bash
ssh root@<your-ip>
curl -fsSL https://get.docker.com | sh
```

Verify:

```bash
docker --version
docker compose version
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

## 5. Set Your Domain in Caddyfile

```bash
nano Caddyfile
```

Replace `your-domain.com` with your actual domain. Example:

```
api.myequityagent.com {
    reverse_proxy api:8000
}
```

---

## 6. Point DNS

In your DNS provider, add an **A record**:

```
your-domain.com  →  <hetzner-public-ip>
```

Caddy requires DNS to resolve before it can obtain a Let's Encrypt TLS certificate. Wait for propagation before starting services.

---

## 7. Add GitHub Secrets

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

## 8. First Deploy

On the server:

```bash
cd /opt/equity-data-agent
docker compose --profile prod up -d --build
```

This will pull images and build the `dagster` and `api` containers. Takes a few minutes on first run.

---

## 9. Run DDL Migrations

Wait for ClickHouse to be healthy, then run migrations:

```bash
# Check ClickHouse is up
docker compose exec clickhouse clickhouse-client --query "SELECT 1"

# Run migrations
make migrate
```

---

## 10. Verify

```bash
# All services running
docker compose ps

# API health check
curl https://your-domain.com/health
```

Expected: all services `Up`, health check returns `200 OK`.

**Acceptance criteria (from QNT-83):**
- [ ] All prod services healthy: `clickhouse`, `dagster`, `dagster-daemon`, `api`, `litellm`, `caddy`
- [ ] `https://your-domain.com/health` returns 200 with valid TLS
- [ ] GitHub Actions CD completes on next push to main
- [ ] ClickHouse databases `equity_raw` and `equity_derived` with all 9 tables exist
- [ ] GitHub secrets configured

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
