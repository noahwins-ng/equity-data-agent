"use client";

/**
 * Watchlist footer next-ingest label.
 *
 * Self-fetches `/api/v1/health` on mount with `cache: "no-store"`.
 * Detached from ISR (QNT-168) so the watchlist server component no
 * longer re-runs on the 5-minute health TTL — the label is status
 * metadata, not first-paint-critical content.
 */

import { useEffect, useState } from "react";

import { API_BASE_URL, type HealthResponse } from "@/lib/api";

export function WatchlistNextIngest() {
  const [label, setLabel] = useState<string>("—");

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_BASE_URL}/api/v1/health`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then((res) => (res.ok ? (res.json() as Promise<HealthResponse>) : null))
      .then((body) => {
        const next = body?.provenance?.jobs?.next_ingest_local;
        if (next) setLabel(next);
      })
      .catch(() => {
        // Soft-fail: stick with the em-dash placeholder.
      });
    return () => controller.abort();
  }, []);

  return <>EOD · {label}</>;
}
