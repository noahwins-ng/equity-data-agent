# frontend

Next.js 16 (App Router) + Tailwind v4 frontend for the Equity Data Agent.

See `docs/design-frontend-plan.md` and `docs/decisions/014-nextjs-rendering-mode-per-page.md` for the canonical Phase 6 design and rendering-mode contract.

## Dev

From the repo root:

```bash
make dev-frontend   # → http://localhost:3001
```

Or from this directory:

```bash
npm run dev         # → http://localhost:3001
npm run lint
npm run typecheck
npm run build
```

## Layout

The app shell (`app/layout.tsx`) is a persistent three-pane grid: left-rail watchlist, middle route slot, right-rail chat panel. Chat is a `"use client"` panel imported into the layout — never a route — so SSE streams survive ticker navigation (ADR-014 §4 + Anti-pattern #6).

| Pane | Owner |
|---|---|
| Left rail (watchlist) | QNT-72 |
| Middle (`/`, `/ticker/[symbol]`) | QNT-72 / QNT-73 |
| Right rail (chat panel) | QNT-74 |
| Vercel deploy | QNT-75 |

## API access

`src/lib/api.ts` is the only module that should call `fetch` against the FastAPI backend. Every call must declare a cache directive (`revalidate: N` or `cache: "no-store"`) — bare `fetch(URL)` is forbidden per ADR-014 Anti-pattern #2.
