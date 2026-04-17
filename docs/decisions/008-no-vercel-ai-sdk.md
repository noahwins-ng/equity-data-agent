# ADR-008: No Vercel AI SDK for the agent chat interface

**Date**: 2026-04-17
**Status**: Accepted

## Context

The frontend is Next.js 15 on Vercel (ADR-005). The default, heavily-marketed option for streaming LLM output into a React UI is the Vercel AI SDK (`ai`, `@ai-sdk/react`). It provides `useChat`, message-shape conventions, tool-call rendering helpers, and server-action integrations.

The agent itself runs in Python (LangGraph) behind a FastAPI SSE endpoint (`POST /api/v1/agent/chat`). The question is whether the frontend should consume that endpoint via the Vercel AI SDK or via native browser primitives.

## Decision

Skip the Vercel AI SDK. Orchestration stays backend-owned — Python/LangGraph produces a well-defined SSE event stream (`tool_call` → `thinking` → `thesis` → `done`), and the Next.js client consumes it with native `fetch` + `ReadableStream`, plus optionally `eventsource-parser` (~2 KB) for line-level SSE parsing.

## Alternatives Considered

**Vercel AI SDK (`useChat` + provider adapter)**
- Zero-boilerplate `useChat` hook, streaming + message state handled.
- Rejected: couples the frontend to the SDK's opinionated message shape and state model. The SDK also assumes the LLM provider is called directly from a Next.js route handler or the edge runtime — but this project's agent is a multi-tool LangGraph loop that must run in Python near ClickHouse and the FastAPI reports. Wrapping a Python SSE endpoint in the SDK's provider abstraction is fighting the framework.

**Vercel AI SDK in "custom transport" mode**
- The SDK does support custom transports.
- Rejected: at that point the SDK adds bundle size and conceptual overhead (its message shape, its streaming protocol) without providing anything `fetch` + `ReadableStream` doesn't. The marginal win over 20 lines of SSE parsing is not worth the lock-in.

**Next.js Server Actions with React Suspense streaming**
- Idiomatic React 19 / Next.js 15.
- Rejected: Server Actions tie the streaming to a specific server component lifecycle. The agent chat UI needs fine-grained event types (tool_call, thinking, thesis, done) rendered differently — Suspense streaming is one-stream-in, one-stream-out. Also, the agent has to stay in Python; a server action would need to proxy the Python SSE endpoint anyway.

## Consequences

**Easier:**
- The SSE contract is the only interface between frontend and backend. Any client (CLI, curl, a future mobile app, the eval harness) that can read SSE works without frontend changes.
- Frontend stays portable — nothing prevents swapping Next.js for another framework later.
- The CLI (QNT-60) hits the same endpoint the browser does during eval runs, so the eval harness exercises the production transport, not a mock.
- No SDK version-upgrade churn on the frontend.

**Harder:**
- ~20 extra lines of SSE parsing code on the frontend (receive chunked text, split on `\n\n`, parse `event:` and `data:` lines). `eventsource-parser` shrinks this further.
- Loses the SDK's built-in tool-call rendering helpers — the frontend handles `tool_call` events manually. This is a feature for this project: the UI decides how to display tool usage, not the SDK.

## Revisiting

Re-open this ADR if the frontend grows multiple independent agent interfaces (chat, inline suggestions, notebooks, etc.) that would share `useChat` state. At that point the SDK's abstractions might start earning their keep.
