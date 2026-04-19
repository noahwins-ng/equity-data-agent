# Uptime Monitoring & Container-State Alerting

Two independent alerting channels that together answer the question *"how do I get paged when production breaks?"* — see QNT-101 for the design rationale.

| Channel | Detects | Latency | Hosted where |
|---|---|---|---|
| **Uptime monitor** (external) | `/health` returns non-200 or is unreachable | alert after 2–3 failed probes (≤3 min) | SaaS or second host |
| **Container-state notifier** (on Hetzner) | `docker events` die/kill/oom/restart on any compose container | ≤30 s | `docker-events-notify.service` on the Hetzner host |

Neither channel alone covers everything. Container exits can happen while `/health` still briefly reports 200 (healthcheck cadence, or a background service crashing). Conversely, the host itself going unreachable is only caught by an external probe. Run both.

---

## 1. Uptime monitor (external `/health` probe)

### 1a. BetterStack free tier (default choice)

Simplest path — free for up to 10 monitors at 3 min interval, mobile app, integrates with Discord/email/SMS. Lives off-host so it still fires when Hetzner itself is down.

1. Sign up at https://betterstack.com/uptime (free tier, no card required).
2. **Create → Monitor**
   - URL: `http://<hetzner-ip>:8000/api/v1/health` (swap for `https://<domain>/api/v1/health` once Caddy is live — Phase 6).
   - Check frequency: **1 min** (tightest on free tier).
   - Request timeout: **10 s**.
   - Expected status: **200**.
   - Recovery period: **2 min** (don't auto-resolve the alert the instant a single probe passes).
   - Escalation: email to `noahwins.dev@gmail.com`, plus a second channel if you want louder.
3. **Alerts → New → Discord**: paste the same webhook URL used for `DISCORD_WEBHOOK_URL` (container-state notifier). Having both channels in one Discord channel means every production event lands in one place.
4. (Optional) **Heartbeat monitor**: create a heartbeat monitor with a 2 min expected interval, copy its push URL into the Hetzner `.env` as `HEARTBEAT_URL`, and restart `docker-events-notify.service`. BetterStack will alert if the notifier stops pinging — i.e. the monitor is monitored.

Verify: `ssh hetzner "docker stop equity-data-agent-api-1"` → alert should arrive within 3 min. Bring it back with `ssh hetzner "cd /opt/equity-data-agent && docker compose --profile prod up -d api"`.

### 1b. uptime-kuma self-hosted (alternative, requires a second host)

Only meaningfully better than BetterStack if you run it on **a different host** than the VPS being monitored (home server, second VPS) — otherwise it shares the same failure domain as what it's probing.

If you have a second host:

```bash
# On the second host (home server, Raspberry Pi, second VPS)
docker run -d --restart=unless-stopped \
  -p 3001:3001 \
  -v uptime-kuma:/app/data \
  --name uptime-kuma \
  louislam/uptime-kuma:1
```

Then open `http://<second-host>:3001`, create an admin, and add an HTTP monitor against `http://<hetzner-ip>:8000/api/v1/health` with the same settings as the BetterStack recipe above. Wire Discord notifications under **Settings → Notifications → Add → Discord**.

Don't run uptime-kuma on the same Hetzner VPS it's monitoring — a host outage takes both down at once and you get no alert.

---

## 2. Container-state notifier (on Hetzner)

Watches `docker events` for die/kill/oom/restart on any container prefixed `equity-data-agent-*` and posts a formatted alert to Discord. Runs as a systemd service on the host so it survives compose-stack restarts.

### 2a. Create the Discord webhook

1. Open a Discord server (create one if needed — free).
2. **Server Settings → Integrations → Webhooks → New Webhook**.
3. Name it `equity-data-agent-prod`, pick the channel that should receive alerts, **Copy Webhook URL**.
4. Add it to `/opt/equity-data-agent/.env` on Hetzner as `DISCORD_WEBHOOK_URL=<url>`. The `.env` file is loaded by the systemd unit via `EnvironmentFile=`.
   - Footgun: systemd's `EnvironmentFile` parser does not strip inline `#` comments. Keep the webhook line bare — `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/…` — and put any comment on a separate line above.

### 2b. Install the service

From the repo root on your laptop:

```bash
make events-notify-install
```

This rsyncs `scripts/docker-events-notify.sh` + the systemd unit to Hetzner, enables the service, and starts it. You should see `[START] docker-events-notify on <hostname>` in Discord within ~10 s.

### 2c. Verify

```bash
# Service status + last heartbeat timestamp
make events-notify-status

# End-to-end test — kills litellm; restart: unless-stopped brings it back.
make events-notify-test
```

Expected in Discord within 30 s:

```
[KILL] `equity-data-agent-litellm-1` exit=137 image=`litellm/litellm:v1.56.5` host=`<hostname>`
```
```
<last 15 log lines>
```

If no message arrives:

1. `make events-notify-status` — is systemd status `active (running)`? Is the heartbeat recent (<90 s old)?
2. `ssh hetzner "journalctl -u docker-events-notify -n 50 --no-pager"` — look for Discord POST errors.
3. Verify `DISCORD_WEBHOOK_URL` is set: `ssh hetzner "grep DISCORD_WEBHOOK_URL /opt/equity-data-agent/.env"`.

---

## 3. Escalation path

Both channels land in the same Discord channel by design — one timeline of production events.

| Alert | Severity | First action | If unresolved |
|---|---|---|---|
| `[OOM KILL]` or repeated `[DIE]` within 5 min | **high** — crash loop | `make check-prod`, read the runbook entry for "Container crash loop" | `ssh hetzner "docker compose --profile prod logs <service> --since 15m"` and open a root-cause ticket |
| BetterStack `/health` down >3 min | **high** — API unreachable | `make check-prod`, follow runbook "API down / 503" | If `/health` returns 503 from ClickHouse down, see runbook "Host reboot outage" or "Container wedged but still up" |
| Single `[KILL]` during a planned deploy | informational | ignore — CD restarts are expected | — |
| BetterStack heartbeat missed | **medium** — notifier itself is dead | `make events-notify-status`, `journalctl -u docker-events-notify` | reinstall: `make events-notify-install` |

For a full failure-mode index, see [`ops-runbook.md`](./ops-runbook.md).
