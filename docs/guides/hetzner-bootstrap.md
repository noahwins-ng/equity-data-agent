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

## 4. Configure Environment (via SOPS)

As of QNT-102, prod secrets are **SOPS-encrypted in git** (`.env.sops`) and decrypted
in the GitHub Actions runner on every deploy. The VPS never holds the age private key
and has no manually-placed `.env` — the file at `/opt/equity-data-agent/.env` is
overwritten by CD on each push to `main`.

For a **first-time setup** (new project or new prod environment), do the SOPS
bootstrap from your dev machine before the first CD run — see §11 below. On the
VPS itself there is nothing to do: skip to §5.

Values you'll encrypt into `.env.sops`:

| Variable | Value |
|---|---|
| `ENV` | `prod` |
| `CLICKHOUSE_HOST` | `clickhouse` (Docker service name — not localhost) |
| `CLICKHOUSE_PORT` | `8123` |
| `LITELLM_BASE_URL` | `http://litellm:4000` (Docker service name — not localhost) |
| `QDRANT_URL` | Your Qdrant Cloud cluster URL |
| `QDRANT_API_KEY` | From Qdrant Cloud dashboard |
| `GROQ_API_KEY` | From [console.groq.com](https://console.groq.com) — default LLM provider, see ADR-011 |
| `GEMINI_API_KEY` | Optional — Gemini 2.5 Flash quality override (free tier, see ADR-011 + QNT-123) |
| `LANGFUSE_PUBLIC_KEY` | From Langfuse dashboard |
| `LANGFUSE_SECRET_KEY` | From Langfuse dashboard |
| `LANGFUSE_BASE_URL` | `https://cloud.langfuse.com` (EU) or `https://us.cloud.langfuse.com` (US) — must match the region where the project was created |
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
| `SOPS_AGE_KEY` | Full contents of `~/.config/sops/age/keys.txt` (both the `# created: …` / `# public key: …` comments and the `AGE-SECRET-KEY-…` line — see §11). Set via: `gh secret set SOPS_AGE_KEY < ~/.config/sops/age/keys.txt`. |

To get your SSH private key:
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

## 11. SOPS secrets management

Prod secrets live in `.env.sops` (committed, encrypted) and are decrypted by the
GitHub Actions runner on every deploy. The runner then `scp`s the plaintext
`.env` to `/opt/equity-data-agent/.env` on the VPS (mode 0600, owned by the
deploy user). The age private key lives only in a password manager and in the
`SOPS_AGE_KEY` GitHub secret — never on the VPS.

### 11.1 One-time project bootstrap (on your dev machine)

Run these once per project, from a clean clone:

```bash
# 1. Install sops + age (macOS; Linux uses the distro package manager)
brew install sops age

# 2. Generate the project's age keypair
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt
chmod 600 ~/.config/sops/age/keys.txt

# 2a. macOS gotcha: sops's default key-file path on macOS is
#     ~/Library/Application Support/sops/age/keys.txt (not ~/.config/sops/age/keys.txt).
#     We keep the Linux path for portability and point sops at it via env var.
#     Skip this block on Linux — the default location is already correct there.
if [ "$(uname)" = "Darwin" ]; then
  export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"
  if ! grep -q 'SOPS_AGE_KEY_FILE' ~/.zshrc 2>/dev/null; then
    echo 'export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"' >> ~/.zshrc
    echo "Added SOPS_AGE_KEY_FILE to ~/.zshrc — reload the shell or use 'source ~/.zshrc'"
  fi
fi

# 3. Copy the public key into .sops.yaml
#    age-keygen prints it to stderr and writes it as a comment inside the file:
#      `# public key: age1xyz...`
#    Replace the `age1REPLACE_WITH_...` placeholder in .sops.yaml with this value.
$EDITOR .sops.yaml

# 4. Create the initial .env locally (if you don't already have one), with all
#    prod values populated. This file never gets committed; it's the plaintext
#    source we're about to encrypt.
$EDITOR .env

# 5. Encrypt it into .env.sops (the committed ciphertext)
#    Use `make sops-encrypt` which passes the required --input-type/--output-type
#    flags; the `.env.sops` filename isn't auto-detected by sops as dotenv
#    because it ends in `.sops`, so every sops invocation needs those flags.
make sops-encrypt

# 6. Verify round-trip works
make sops-decrypt | diff - .env && echo "round-trip OK"

# 7. Escrow the private key. Goal: a copy that survives your laptop dying
#    and is INDEPENDENT of the GH secret. Any of:
#      - Apple Notes locked entry titled "equity-data-agent / sops age key"
#        (built-in, free, end-to-end encrypted in iCloud when locked).
#        `pbcopy < ~/.config/sops/age/keys.txt` then paste; File → Lock Note.
#      - Bitwarden Secure Note (free tier).
#      - 1Password Secure Note.
#      - Printed paper in a fireproof box (most disaster-resistant).
#    Always paste the ENTIRE contents of ~/.config/sops/age/keys.txt
#    (including the `# created: …` / `# public key: …` comments — SOPS needs
#    the whole file to reconstruct identity).

# 8. Add the SOPS_AGE_KEY GitHub secret. Value = full keys.txt contents:
gh secret set SOPS_AGE_KEY < ~/.config/sops/age/keys.txt

# 9. Commit .sops.yaml and .env.sops
git add .sops.yaml .env.sops
git commit -m "QNT-102: feat(infra): SOPS-encrypt .env secrets at rest"
```

### 11.2 Editing secrets (rotation of individual values)

```bash
# Opens .env.sops in $EDITOR with values transparently decrypted.
# On save, SOPS re-encrypts and rewrites the file.
make sops-edit          # wraps: sops --input-type dotenv --output-type dotenv .env.sops

# Commit the re-encrypted ciphertext and push; CD picks it up on next deploy.
git commit -am "QNT-XX: chore(secrets): rotate <key name>"
git push
```

**Container-recreation gotcha**: Docker Compose only recreates a container when
the *service definition* changes (image SHA, command, env_file path, ports,
volumes). Editing the *contents* of `.env` does NOT count — the running
container keeps the env vars it was launched with. So a value-only rotation
push needs a force-recreate to take effect:

```bash
ssh hetzner "cd /opt/equity-data-agent && \
    docker compose --profile prod up -d --force-recreate <service-name>"
```

Pick the service that consumes the rotated value (e.g. `litellm` for an LLM
provider key, `api` for a Sentry DSN, `dagster` for ClickHouse host changes).
If you're rotating a value used by *every* service, recreate the whole stack:
`docker compose --profile prod up -d --force-recreate`.

Verify the new value is active inside the container:
```bash
ssh hetzner "docker exec equity-data-agent-<service>-1 printenv <KEY>"
```

If the rotation is part of a code change push, no manual recreate is needed —
the code change rebuilds the image, which docker compose treats as a service
change and recreates the container automatically.

### 11.3 Rotating the age key itself

Do this if the private key is suspected leaked (laptop theft, accidental push,
etc.) or on a scheduled rotation.

```bash
# 1. Generate a new keypair
age-keygen -o ~/.config/sops/age/keys.new.txt

# 2. Update .sops.yaml with the new public key
#    (keep the old recipient listed temporarily if you want to decrypt history;
#    otherwise replace outright)
$EDITOR .sops.yaml

# 3. Re-encrypt .env.sops under the new key
#    `sops updatekeys` rewrites the sops metadata without changing plaintext:
make sops-rotate-keys     # wraps: sops updatekeys .env.sops

# 4. Swap the active key file
mv ~/.config/sops/age/keys.txt ~/.config/sops/age/keys.old.txt
mv ~/.config/sops/age/keys.new.txt ~/.config/sops/age/keys.txt

# 5. Update the GitHub secret SOPS_AGE_KEY to the new keys.txt contents
gh secret set SOPS_AGE_KEY < ~/.config/sops/age/keys.txt

# 6. Update the escrow entry (Apple Notes / Bitwarden / 1Password / etc.)
#    to the new keys.txt contents. Don't delete the old entry until step 7
#    succeeds — the old key is still needed if the new CD run fails.

# 7. Commit and push — next CD run will use the new key
git add .sops.yaml .env.sops
git commit -m "QNT-XX: chore(secrets): rotate SOPS age key"
git push

# 8. After the first successful CD run on the new key, destroy the old key
shred -u ~/.config/sops/age/keys.old.txt
```

If a secret VALUE was also suspected leaked (not just the age key), rotate it
at the upstream (Anthropic dashboard, Qdrant dashboard, etc.) first, then run
§11.2 to update `.env.sops` with the new value.

### 11.4 Recovering from a lost age key

If the private key is gone and was not escrowed:

1. Rotate every secret value at its upstream provider — you've lost the
   ability to decrypt `.env.sops`, so treat all stored values as destroyed.
2. Generate a new age keypair (§11.1 steps 2-3).
3. Build a fresh `.env` with the new upstream values.
4. Encrypt it under the new key (§11.1 step 5, overwriting `.env.sops`).
5. Update the GitHub secret and password manager (§11.1 steps 7-8).
6. Commit and push.

The encrypted history in git is now orphaned — no key can decrypt it. That's
fine; the point is that no one else can decrypt it either.

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
