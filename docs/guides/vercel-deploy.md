# Vercel Deploy Guide

End-to-end deploy of the Next.js frontend on Vercel, talking to the Hetzner FastAPI through a Cloudflare quick tunnel. See `docs/decisions/018-cloudflare-quick-tunnel-for-https-ingress.md` for the architecture rationale.

## Topology

```
Browser → https://<vercel-project>.vercel.app   (Vercel CDN — frontend)
Browser → https://<random>.trycloudflare.com    (Cloudflare edge — API)
                                │
                                ▼ (cloudflared, outbound from Hetzner)
                          api:8000 (FastAPI, loopback-bound)
```

## Prerequisites

- Vercel account (free tier).
- Hetzner host with `docker compose` and the repo deployed.
- Cloudflare account NOT required (quick tunnel is anonymous).

## Steps

### 1. Land QNT-75 to `main` first

The `cloudflared` service definition lives in `docker-compose.yml` and only ships with the QNT-75 PR. There is no way to bring up cloudflared on Hetzner until that PR merges and CD runs.

Sequence:

```bash
# From the QNT-75 branch, on your laptop:
gh pr create --title "QNT-75: ..." --body "Closes QNT-75"
# Wait for CI green, squash-merge.
# CD runs `docker compose --profile prod up -d --build` and starts cloudflared
# alongside the rest of the prod stack.
```

Track CD: `gh run list --branch main --limit 3` should show the deploy completing in ~1-2 min.

### 2. One-time cleanup of the orphaned `caddy` container

Pre-QNT-75, `caddy` ran under the `prod` profile. ADR-018 moves it to a dormant `prod-caddy` profile. `docker compose up --remove-orphans` only removes containers whose service is *undefined* in compose; a service that's defined but excluded by profile is preserved by design. So the existing caddy container survives the deploy and keeps holding ports 80/443 + ~256 MB of RAM until you stop it manually:

```bash
ssh hetzner "docker stop caddy && docker rm -f caddy"
```

One-time only — once removed, future deploys won't recreate it (caddy is no longer in the active profile).

### 3. Read the tunnel hostname from cloudflared logs

```bash
ssh hetzner "docker logs equity-data-agent-cloudflared-1 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1"
# → https://random-words-here.trycloudflare.com
```

Save that URL — it's what Vercel will call AND what UptimeRobot will probe (see step 5).

### 4. Verify the tunnel works

From your laptop (no SSH tunnel needed — it's now public via Cloudflare):

```bash
curl -sf https://random-words-here.trycloudflare.com/api/v1/health | jq .status
# → "ok"
```

If this returns `ok`, the tunnel is live and the API is reachable.

### 5. Update UptimeRobot probe URL

Pre-QNT-75 the external uptime probe pointed at `http://<hetzner-ip>:8000/api/v1/health`. That endpoint is now unreachable (api is bound to loopback) — UptimeRobot will start firing DOWN alerts within a check interval if not updated.

In the UptimeRobot dashboard → your monitor → **Edit** → swap the URL to:

```
https://<your-trycloudflare-url>/api/v1/health
```

Save. The probe should go green within one check interval.

When the trycloudflare URL rotates (see "When the trycloudflare URL rotates" below), update UptimeRobot at the same time as Vercel — both reference the same hostname.

### 6. Update prod CORS allowlist

The FastAPI CORS middleware (QNT-161) only allows origins listed in `CORS_ALLOWED_ORIGINS`. Add the Vercel project domain.

On your laptop, edit the encrypted env file:

```bash
sops .env.sops
```

Add (or update):

```dotenv
CORS_ALLOWED_ORIGINS=https://<your-vercel-project>.vercel.app,http://localhost:3001
CORS_ALLOWED_ORIGIN_REGEX=^https://<your-vercel-project>(-[a-z0-9-]+)?\.vercel\.app$
```

The regex matches Vercel preview deploys for *this* project only — leaked previews from unrelated projects can't drive traffic. Replace `<your-vercel-project>` with the actual project slug.

Commit, push, and let CD pick it up — the deploy workflow decrypts `.env.sops` and writes it to the Hetzner host as `.env` before `docker compose up`.

### 7. Link the Vercel project

From your laptop:

```bash
cd frontend
npx vercel link
```

Follow the prompts:
- Scope: your personal account.
- Link to existing project: **No** (first-time setup).
- Project name: choose one (this becomes `<name>.vercel.app`).
- Directory: `./` (you're already in `frontend/`).
- Override build settings: **No**.

This creates `frontend/.vercel/project.json` (gitignored).

### 8. Configure Vercel env vars

In the Vercel dashboard → your project → **Settings → Environment Variables**, add:

| Name | Value | Scope |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `https://random-words-here.trycloudflare.com` (from step 1) | Production, Preview |

The `NEXT_PUBLIC_` prefix exposes it to the browser bundle — this is intentional, the browser needs to know the API URL to fetch from. No other env vars are needed; everything else is server-side and stays on Hetzner.

### 9. Configure repo root

In **Settings → General → Root Directory**, set to `frontend`. Vercel runs `npm install` and `next build` from that directory.

### 10. First deploy

Push to `main` or trigger a preview deploy:

```bash
# From the repo root, on a feature branch:
git push origin <your-branch>
# Vercel creates a preview deploy automatically.
```

Or deploy production directly:

```bash
cd frontend && npx vercel --prod
```

### 11. Verify

- Open `https://<your-vercel-project>.vercel.app` in a browser.
- Watchlist should load (server-side fetch from Hetzner via the tunnel).
- Click a ticker — chart, fundamentals, news should populate.
- Open the chat panel, send a message — SSE should stream tool calls + thesis.
- Open browser devtools → Network tab → confirm:
  - All API calls go to `https://*.trycloudflare.com/api/v1/...`
  - No CORS errors in console.
  - No mixed-content warnings.

## When the trycloudflare URL rotates

`cloudflared` restarts generate a new `*.trycloudflare.com` hostname. Triggers:

- Kernel reboots / container crashes (rare).
- Deliberate `cloudflare/cloudflared` image-tag bump in `docker-compose.yml` (the image is pinned, so this only happens when you edit the version).

Symptoms:

- Frontend loads but every API call returns a network error.
- `/api/v1/health` from the laptop returns connection refused on the old hostname.

Recovery (~5 min):

```bash
# 1. Read the new URL
ssh hetzner "docker logs cloudflared 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1"

# 2. Update Vercel env var
#    Dashboard → Settings → Environment Variables → edit NEXT_PUBLIC_API_URL
#    Or via CLI:
cd frontend
npx vercel env rm NEXT_PUBLIC_API_URL production
npx vercel env add NEXT_PUBLIC_API_URL production
# (paste the new URL)

# 3. Redeploy
npx vercel --prod
```

The frontend re-bundles with the new URL and starts working again.

## Troubleshooting

**`/api/v1/health` returns connection refused over the trycloudflare URL**
- `cloudflared` not running: `ssh hetzner "docker ps | grep cloudflared"`. Restart with `docker compose --profile prod up -d cloudflared`.
- API not running: `ssh hetzner "docker ps | grep ' api'"`. Check logs.

**CORS error in browser console**
- The Vercel domain isn't in `CORS_ALLOWED_ORIGINS`. See step 3.
- Check the request URL — if it's the trycloudflare hostname (server-side fetch on Vercel doesn't have an origin) the issue is elsewhere; if it's a browser fetch, the origin should be `https://<your-vercel-project>.vercel.app`.

**Preview deploy works but production doesn't**
- `CORS_ALLOWED_ORIGIN_REGEX` covers `<project>-<branch>.vercel.app` for previews; the production domain `<project>.vercel.app` must be in `CORS_ALLOWED_ORIGINS` separately. Verify both.

**SSE chat disconnects mid-stream**
- Cloudflare quick tunnels don't have a documented streaming timeout, but if you see disconnects, check `docker logs cloudflared` for connection resets.
- The agent's QNT-150 cleanup ensures graceful shutdown; symptoms here would be a reconnect loop in the browser.

**Vercel build fails on first deploy**
- Root directory not set to `frontend` — see step 6.
- Missing `NEXT_PUBLIC_API_URL` — Vercel build doesn't strictly require it (server fetches gracefully fail with `IS_PRERENDER` per `lib/api.ts`), but pages will be empty if not set.
