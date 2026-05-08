# Vercel Deploy Guide

End-to-end deploy of the Next.js frontend on Vercel, talking to the Hetzner FastAPI through a Cloudflare named tunnel. See `docs/decisions/018-cloudflare-quick-tunnel-for-https-ingress.md` for the architecture rationale (note: the ADR was originally written for the quick-tunnel mode and superseded on 2026-05-08 by QNT-177's switch to a named tunnel; the Update section at the end of the ADR captures the current state).

## Topology

```
Browser → https://<vercel-project>.vercel.app   (Vercel CDN — frontend)
Browser → https://api.<your-domain>             (Cloudflare edge — named tunnel)
                                │
                                ▼ (cloudflared, outbound from Hetzner)
                          api:8000 (FastAPI, loopback-bound)
```

## Prerequisites

- Vercel account (free tier).
- Hetzner host with `docker compose` and the repo deployed.
- Cloudflare account with a domain in the zone (free plan is sufficient).

## Steps

### 1. Create the named tunnel in Cloudflare

In the Cloudflare dashboard:

1. Zero Trust → Networks → Tunnels → **Create a tunnel** → type `Cloudflared` → name `equity-data-agent` → Save.
2. On the install page, **copy the connector token** (the long `eyJ...` string). Skip the install command — the token is wired in via Docker, not by running cloudflared on the host.
3. Public Hostname tab → **Add a public hostname**:
   - Subdomain: `api`
   - Domain: pick `<your-domain>` from the dropdown
   - Path: leave blank
   - Service Type: `HTTP`
   - URL: `api:8000`
4. Save. Cloudflare auto-creates `api.<your-domain>` as a CNAME to the tunnel UUID (proxied / orange-cloud).

### 2. Add the token to `.env.sops`

The `cloudflared` service reads `CLOUDFLARE_TUNNEL_TOKEN` from the environment. Add it to the encrypted env file on your laptop:

```bash
make sops-edit
```

Add the line:

```dotenv
CLOUDFLARE_TUNNEL_TOKEN=eyJ...
```

Save. SOPS re-encrypts in place. Commit `.env.sops` along with any compose changes.

### 3. Deploy and verify

CD picks up the encrypted file on merge to `main`, decrypts it in the GitHub Actions runner (per QNT-102), scp's plaintext to `/opt/equity-data-agent/.env`, and runs `docker compose --profile prod up -d --build --remove-orphans`. cloudflared starts with the token, registers with Cloudflare, and the public hostname comes online.

Verify from your laptop (no SSH tunnel needed — the API is now public via Cloudflare):

```bash
curl -sf https://api.<your-domain>/api/v1/health | jq .status
# → "ok"
```

### 4. Update UptimeRobot probe URL

In the UptimeRobot dashboard → your monitor → **Edit** → set the URL to:

```
https://api.<your-domain>/api/v1/health
```

Save. The probe should go green within one check interval. The URL is permanent — no more rotation maintenance.

### 5. Update prod CORS allowlist

The FastAPI CORS middleware (QNT-161) only allows origins listed in `CORS_ALLOWED_ORIGINS`. Add the Vercel project domain:

```bash
make sops-edit
```

Add (or update):

```dotenv
CORS_ALLOWED_ORIGINS=https://<your-vercel-project>.vercel.app,http://localhost:3001
CORS_ALLOWED_ORIGIN_REGEX=^https://<your-vercel-project>(-[a-z0-9-]+)?\.vercel\.app$
```

The regex matches Vercel preview deploys for *this* project only — leaked previews from unrelated projects can't drive traffic. Replace `<your-vercel-project>` with the actual project slug.

Commit, push, and let CD pick it up.

### 6. Link the Vercel project

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

### 7. Configure Vercel env vars

In the Vercel dashboard → your project → **Settings → Environment Variables**, add:

| Name | Value | Scope |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `https://api.<your-domain>` | Production, Preview |

The `NEXT_PUBLIC_` prefix exposes it to the browser bundle — this is intentional, the browser needs to know the API URL to fetch from. No other env vars are needed; everything else is server-side and stays on Hetzner.

### 8. Configure repo root

In **Settings → General → Root Directory**, set to `frontend`. Vercel runs `npm install` and `next build` from that directory.

### 9. First deploy

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

### 10. Verify

- Open `https://<your-vercel-project>.vercel.app` in a browser.
- Watchlist should load (server-side fetch from Hetzner via the tunnel).
- Click a ticker — chart, fundamentals, news should populate.
- Open the chat panel, send a message — SSE should stream tool calls + thesis.
- Open browser devtools → Network tab → confirm:
  - All API calls go to `https://api.<your-domain>/api/v1/...`
  - No CORS errors in console.
  - No mixed-content warnings.

## Reboot survival

The named tunnel registers using the connector token, which is stable. Host reboots (kernel updates, manual restarts) are non-events for the public hostname:

1. Hetzner reboots → docker daemon comes back → cloudflared container starts with the same token → reconnects to the same tunnel UUID → public hostname stays bound.
2. No Vercel env var refresh required.
3. Optional chaos test post-deploy: `ssh hetzner sudo reboot`, wait 2 min, re-curl `https://api.<your-domain>/api/v1/health` and a Vercel `/ticker/<symbol>` page — both should return 200 without intervention.

## Troubleshooting

**`/api/v1/health` returns connection refused over `api.<your-domain>`**
- `cloudflared` not running: `ssh hetzner "docker ps | grep cloudflared"`. Restart with `docker compose --profile prod up -d cloudflared`.
- Token mismatch: `docker logs cloudflared` shows authentication failure. Verify `CLOUDFLARE_TUNNEL_TOKEN` in `.env.sops` matches the one shown in Cloudflare Zero Trust → your tunnel → Configure tab.
- Public hostname missing: in the Cloudflare dashboard, the Public Hostname tab must show `api.<your-domain>` → `http://api:8000`. If empty, requests reach the tunnel but get no route and Cloudflare returns 530.
- API not running: `ssh hetzner "docker ps | grep ' api'"`. Check logs.

**CORS error in browser console**
- The Vercel domain isn't in `CORS_ALLOWED_ORIGINS`. See step 5.
- Check the request URL — if it's `api.<your-domain>` (server-side fetch on Vercel doesn't have an origin) the issue is elsewhere; if it's a browser fetch, the origin should be `https://<your-vercel-project>.vercel.app`.

**Preview deploy works but production doesn't**
- `CORS_ALLOWED_ORIGIN_REGEX` covers `<project>-<branch>.vercel.app` for previews; the production domain `<project>.vercel.app` must be in `CORS_ALLOWED_ORIGINS` separately. Verify both.

**SSE chat disconnects mid-stream**
- Cloudflare named tunnels carry SSE without enforced timeouts at the ingress layer. If you see disconnects, check `docker logs cloudflared` for connection resets.
- The agent's QNT-150 cleanup ensures graceful shutdown; symptoms here would be a reconnect loop in the browser.

**Vercel build fails on first deploy**
- Root directory not set to `frontend` — see step 8.
- Missing `NEXT_PUBLIC_API_URL` — Vercel build doesn't strictly require it (server fetches gracefully fail with `IS_PRERENDER` per `lib/api.ts`), but pages will be empty if not set.
