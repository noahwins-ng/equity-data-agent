# ADR-005: Next.js + Vercel Over Python-Native Frontend

**Date**: 2026-04-13
**Status**: Accepted

## Context

The project needed a frontend to present equity research data — ticker dashboards, multi-timeframe charts, fundamental ratios, and an agent chat interface. The backend is Python-heavy (Dagster, FastAPI, LangGraph), so a Python-native UI was a natural first consideration.

The key requirements:
- Financial candlestick charts with technical indicator overlays (RSI, MACD, Bollinger Bands) across daily/weekly/monthly timeframes
- A real-time streaming chat interface for agent thesis generation
- Professional enough presentation to serve as a portfolio showcase
- Zero-friction deployment

## Decision

Use **Next.js 15** (App Router) + **Tailwind CSS** for the frontend, deployed on **Vercel**.

Charts are rendered with **TradingView Lightweight Charts v5** — free, Apache 2.0, purpose-built for financial charting, with native multi-pane support for RSI/MACD panes.

Agent chat uses **Server-Sent Events (SSE)** from a FastAPI endpoint, consumed natively by the Next.js client.

## Alternatives Considered

**Streamlit**
- Python-only — no context switch from the backend stack
- Fast to build basic dashboards
- Rejected: no native candlestick/financial charting, very limited layout control, looks like a prototype rather than a product. Multi-pane interactive charts with TradingView would require complex workarounds.

**Gradio**
- Excellent for AI demo interfaces
- Rejected: too narrow — purpose-built for ML demos, not for a multi-page financial dashboard. Dashboard and chart pages would be awkward.

**FastAPI + HTMX + Jinja2**
- Stays in Python, server-rendered, no JS framework
- Rejected: insufficient interactivity for TradingView chart integration (requires client-side JS), streaming agent output is harder, and it would require building chart components from scratch.

**React SPA (Vite/CRA)**
- Full control, good charting library ecosystem
- Rejected: Next.js is strictly better — SSR/SSG for dashboard data, file-based routing, Vercel-native deployment. No reason to pick a bare React SPA over Next.js 15 for a new project.

## Consequences

**Easier:**
- TradingView Lightweight Charts v5 integrates naturally as a React component
- SSE streaming from FastAPI works natively with `fetch` + `ReadableStream` in Next.js
- Vercel auto-deploys on push to `main`, preview deploys on PRs — zero deployment configuration
- Portfolio quality: looks professional, not a prototype

**Harder:**
- Adds TypeScript/JavaScript to the stack alongside Python — two languages to context-switch between
- CORS configuration required between Vercel (frontend) and Hetzner (FastAPI backend)
- `NEXT_PUBLIC_API_URL` environment variable must be configured correctly for dev vs prod
- Next.js 15 breaking changes from 14: no default fetch caching, async request APIs, React 19 (minor migration effort if upgrading later)
