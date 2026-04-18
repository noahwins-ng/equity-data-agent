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

## 10. Unattended-Upgrades Mail Alerts (Resend SMTP Relay)

`unattended-upgrades` silently schedules a host reboot when a kernel/libc update requires one. On 2026-04-18 this went unnoticed for ~21 hours — see the QNT-95 / QNT-96 incident — so we require mail delivery on every upgrade run. Pending reboots are also surfaced independently by `scripts/health-monitor.sh` (§10.6 below), so the two channels are belt-and-suspenders.

### 10.1 Why Resend (and not direct SMTP or Gmail)

Three options were considered during QNT-96:

| Option | Rejected / Chosen | Reason |
|---|---|---|
| **Direct SMTP to the recipient MX** (postfix → gmail.com on port 25) | Rejected | Hetzner blocks outbound port 25 by default (anti-spam policy; requires a support ticket to unblock). Even with the block lifted, Gmail aggressively spam-filters unauthenticated VPS senders without SPF/DKIM. |
| **Gmail SMTP relay** (`smtp.gmail.com:587` with a Google App Password) | Rejected | Requires 2FA enabled on the Google account and an App Password generated from account settings — not available in our environment. |
| **Resend SMTP relay** (`smtp.resend.com:587` with an API key) | **Chosen** | Free tier (3k/month, 100/day), simple API-key auth, port 587 with STARTTLS so Hetzner's port-25 block is irrelevant, good deliverability without DNS setup. |

### 10.2 Install the mail transport

`bsd-mailx` provides the `mail` command that `unattended-upgrades` invokes. It pulls in `postfix` as the MTA.

```bash
# Preseed postfix so debconf doesn't prompt interactively
HN=$(hostname -f)
sudo debconf-set-selections <<EOF
postfix postfix/main_mailer_type select Internet Site
postfix postfix/mailname string $HN
postfix postfix/destinations string $HN, localhost.localdomain, localhost
EOF
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y bsd-mailx
```

**Gotcha that cost an hour during QNT-96**: if postfix is configured as `Local only` (the default if preseeding is skipped), it silently sets `default_transport = error` which bounces *everything* even after you add a relayhost. The symptom is `relay=none, status=bounced` in `/var/log/mail.log` with zero connection delay. Fix explicitly:

```bash
sudo postconf -e 'default_transport = smtp' 'relay_transport = smtp'
```

### 10.3 Configure postfix as a Resend smarthost

Generate an API key at https://resend.com → **API Keys** → "Sending access". Then:

```bash
# 1. Relay + sender-rewrite config
sudo postconf -e \
  'relayhost = [smtp.resend.com]:587' \
  'smtp_sasl_auth_enable = yes' \
  'smtp_sasl_security_options = noanonymous' \
  'smtp_sasl_password_maps = hash:/etc/postfix/sasl/sasl_passwd' \
  'smtp_tls_security_level = encrypt' \
  'smtp_tls_CAfile = /etc/ssl/certs/ca-certificates.crt' \
  'inet_interfaces = loopback-only' \
  'inet_protocols = ipv4' \
  'sender_canonical_classes = envelope_sender, header_sender' \
  'sender_canonical_maps = regexp:/etc/postfix/sender_canonical' \
  'default_transport = smtp' \
  'relay_transport = smtp'

# 2. Sender rewrite — Resend's free tier requires a verified-domain sender.
# `onboarding@resend.dev` is their pre-verified default; use it until you
# verify your own domain (DNS TXT records via the Resend dashboard).
echo '/.+/    onboarding@resend.dev' | sudo tee /etc/postfix/sender_canonical > /dev/null

# 3. SASL credentials — root-only, 0600. SMTP username is literally "resend".
sudo mkdir -p /etc/postfix/sasl
sudo sh -c 'cat > /etc/postfix/sasl/sasl_passwd <<EOF
[smtp.resend.com]:587 resend:<YOUR_RESEND_API_KEY>
EOF'
sudo chmod 600 /etc/postfix/sasl/sasl_passwd
sudo postmap /etc/postfix/sasl/sasl_passwd          # creates .db hash map
sudo chmod 600 /etc/postfix/sasl/sasl_passwd.db

sudo systemctl restart postfix
```

**Free-tier caveat**: without domain verification, Resend only accepts mail **to** the email associated with your Resend account. For us that's `noahwins.dev@gmail.com`, which matches our `Unattended-Upgrade::Mail` target below — so it works. Domain verification lifts both the sender-rewrite requirement and the single-recipient restriction, and is worth doing once a project domain is available.

### 10.4 Enable unattended-upgrades mail

In `/etc/apt/apt.conf.d/50unattended-upgrades`, uncomment + set:

```
Unattended-Upgrade::Mail "noahwins.dev@gmail.com";
Unattended-Upgrade::MailReport "on-change";
```

`on-change` emits a mail only when packages were actually upgraded or an error occurred — not on every no-op run.

### 10.5 Verify delivery end-to-end

```bash
echo "test from $(hostname) at $(date -u +%FT%TZ)" | mail -s "hetzner mail test" noahwins.dev@gmail.com

# Expected in /var/log/mail.log (the key string is "status=sent"):
#   postfix/smtp[...]: <queue-id>: to=<noahwins.dev@gmail.com>,
#     relay=smtp.resend.com[...]:587, status=sent (250 <resend-message-id>)
sudo tail -5 /var/log/mail.log
```

Then check the Gmail inbox — including Promotions and Spam the first time, since `onboarding@resend.dev` is a new sender to that inbox and Gmail may not route to Primary initially.

**Mail that never leaves the box is worse than no alerting** — always run this check after any smarthost config change. `relay=none, status=bounced` with zero delay means postfix rejected the routing locally (usually the `default_transport = error` gotcha from §10.2). A real SMTP failure will show non-zero `delays=...` and a reason string.

### 10.6 Pending-reboot surfacing (independent channel)

`scripts/health-monitor.sh` (cron, every 15 min) logs a `REBOOT REQUIRED: <package-list>` line whenever `/var/run/reboot-required` exists. That line is surfaced by:

- `make monitor-log` (operator-initiated)
- The Claude Code session-start hook (auto-warns on new session)

So even if Resend were down or the API key were rotated without updating postfix, pending reboots would still become visible within one cron tick.

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
