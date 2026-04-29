/**
 * Typed fetch wrapper for the FastAPI backend.
 *
 * Per ADR-014 Anti-pattern #2: every server-side fetch must declare a cache
 * directive. Bare fetch(URL) is forbidden — Next.js 15+ dropped default
 * caching, so an unannotated fetch re-hits the API on every navigation.
 *
 * Usage:
 *   const data = await apiFetch<DashboardSummary>("/api/v1/dashboard/summary");
 *   // SSE: use apiFetchRaw to keep the Response body as a ReadableStream.
 *   const res = await apiFetchRaw("/api/v1/agent/chat", { cache: "no-store", method: "POST", body });
 *
 * Cache vocabulary:
 *   - revalidate: number  → ISR / Data Cache TTL in seconds (default for daily data)
 *   - cache: "no-store"   → opt out (SSE, per-request data, client toggles)
 *   - cache: "force-cache" → cache indefinitely (rare)
 */

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** Default revalidation window for daily-cadence data (60 s — see ADR-014 §1). */
export const DEFAULT_REVALIDATE_SECONDS = 60;

export type ApiFetchOptions = Omit<RequestInit, "cache"> & {
  /** ISR / Data Cache TTL in seconds. Defaults to 60. Mutually exclusive with `cache`. */
  revalidate?: number;
  /** Override Next's data cache (e.g. "no-store" for SSE). Mutually exclusive with `revalidate`. */
  cache?: RequestCache;
  /** Cache tags for on-demand revalidation via revalidateTag(). */
  tags?: string[];
};

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly path: string,
    message: string,
  ) {
    super(`[${status}] ${path}: ${message}`);
    this.name = "ApiError";
  }
}

/**
 * Fetch JSON from the API with explicit cache semantics.
 * Throws ApiError on non-2xx responses.
 */
export async function apiFetch<T>(
  path: string,
  options: ApiFetchOptions = {},
): Promise<T> {
  const response = await apiFetchRaw(path, options);
  if (!response.ok) {
    const body = await response.text().catch(() => "<no body>");
    throw new ApiError(response.status, path, body);
  }
  return response.json() as Promise<T>;
}

/**
 * Fetch raw Response from the API (use for SSE / streaming endpoints).
 * Caller is responsible for inspecting `response.ok` and reading the body.
 */
export async function apiFetchRaw(
  path: string,
  options: ApiFetchOptions = {},
): Promise<Response> {
  const { revalidate, cache, tags, ...rest } = options;

  if (revalidate !== undefined && cache !== undefined) {
    throw new Error(
      "apiFetch: pass either `revalidate` or `cache`, not both — they conflict.",
    );
  }

  const init: RequestInit = { ...rest };

  if (cache !== undefined) {
    init.cache = cache;
  } else {
    init.next = {
      revalidate: revalidate ?? DEFAULT_REVALIDATE_SECONDS,
      ...(tags ? { tags } : {}),
    };
  }

  return fetch(`${API_BASE_URL}${path}`, init);
}

export { API_BASE_URL };
