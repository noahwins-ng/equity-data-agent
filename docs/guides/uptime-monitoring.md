# Uptime Monitoring & Container-State Alerting

Two independent alerting channels that together answer the question *"how do I get paged when production breaks?"* — see QNT-101 for the design rationale.

| Channel | Detects | Latency | Hosted where |
|---|---|---|---|
| **Uptime monitor** (external) | `/health` returns non-200 or is unreachable | alert after 2 failed probes (≤10 min on UptimeRobot free, ≤3 min on BetterStack free) | SaaS or second host |
| **Container-state notifier** (on Hetzner) | `docker events` die/kill/oom/restart on any compose container | ≤30 s | `docker-events-notify.service` on the Hetzner host |

Neither channel alone covers everything. Container exits can happen while `/health` still briefly reports 200 (healthcheck cadence, or a background service crashing). Conversely, the host itself going unreachable is only caught by an external probe. Run both.

---

## 1. Uptime monitor (external `/health` probe)

### 1a. UptimeRobot free tier (default choice)

Simplest path **with native Discord integration on free tier** — free for 50 monitors at 5-min probe interval, 5 integration instances included, iOS/Android app. Lives off-host so it still fires when Hetzner itself is down.

1. Sign up at https://uptimerobot.com (free tier, no card required).
2. **Dashboard → + Add New Monitor**
   - Monitor Type: **HTTP(s)**
   - Friendly Name: `equity-data-agent prod API`
   - URL: `http://<hetzner-ip>:8000/api/v1/health` (swap for `https://<domain>/api/v1/health` once Caddy is live — Phase 6).
   - Monitoring Interval: **5 min** (tightest on free tier).
   - Monitor Timeout: **30 s**.
   - **HTTP status codes**: alert on anything outside `200`. Don't use keyword matching — `/health` returns HTTP 200 with `status:"degraded"` when Qdrant is down, and we don't want to page on that.
3. **My Settings → Alert Contacts → Add Alert Contact → Discord**: paste the same webhook URL used for `DISCORD_WEBHOOK_URL` (container-state notifier). Having both channels in one Discord channel means every production event lands in one place. Attach the alert contact to the monitor created in step 2.
4. **Self-monitoring (optional but recommended)**: UptimeRobot's free tier doesn't include heartbeat monitors. Baseline is always available: the notifier writes `/opt/equity-data-agent/events-notify-heartbeat` every 60 s, and `make events-notify-status` shows its age — operator-polled. For push-based alerting use [Healthchecks.io](https://healthchecks.io) (free tier: 20 checks):
   - Create a check with "period" = 2 min, "grace" = 1 min.
   - Copy the check's ping URL into `/opt/equity-data-agent/.env` as `HEARTBEAT_URL`.
   - `ssh hetzner "systemctl restart docker-events-notify"` to pick up the new env.
   - Route alerts to Discord: Healthchecks.io supports generic webhook notifications — point one at your Discord webhook URL with body `{"content": "Heartbeat missed for $NAME"}`. Or use their email integration and accept a second notification surface.

Verify: `ssh hetzner "docker stop equity-data-agent-api-1"` → alert should arrive within ≤10 min worst case (5-min interval × 2 failed checks). Bring it back with `ssh hetzner "cd /opt/equity-data-agent && docker compose --profile prod up -d api"`.

### 1b. Alternatives

- **BetterStack free tier** — tighter 3-min probe interval and better incident UX, but the free tier only supports email + Slack (Discord requires a paid plan). Reasonable choice if you live in Slack or are willing to wire a custom webhook payload targeting your Discord webhook URL. Signup at https://betterstack.com/uptime.
- **uptime-kuma self-hosted (on a *second* host)** — native Discord and unlimited monitors, but only meaningfully better than UptimeRobot if run on a **different host** (home server, second VPS) than the VPS being monitored. Same-host install shares the failure domain of what it's probing — if Hetzner is unreachable, so is kuma.
  ```bash
  # On a second host (home server, Raspberry Pi, second VPS)
  docker run -d --restart=unless-stopped \
    -p 3001:3001 \
    -v uptime-kuma:/app/data \
    --name uptime-kuma \
    louislam/uptime-kuma:1
  ```
  Then open `http://<second-host>:3001`, create an admin, add an HTTP monitor against `/api/v1/health`, wire Discord under **Settings → Notifications → Add → Discord**.

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
| Uptime monitor reports `/health` down | **high** — API unreachable | `make check-prod`, follow runbook "API down / 503" | If `/health` returns 503 from ClickHouse down, see runbook "Host reboot outage" or "Container wedged but still up" |
| Single `[KILL]` during a planned deploy | informational | ignore — CD restarts are expected | — |
| Heartbeat-monitor alert (Healthchecks.io or equivalent) | **medium** — notifier itself is dead | `make events-notify-status`, `journalctl -u docker-events-notify` | reinstall: `make events-notify-install` |

For a full failure-mode index, see [`ops-runbook.md`](./ops-runbook.md).
