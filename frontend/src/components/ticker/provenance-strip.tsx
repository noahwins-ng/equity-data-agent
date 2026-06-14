"use client";

/**
 * Provenance strip — bottom of the ticker detail middle pane.
 *
 * Two rows: SOURCES and JOBS, both data-driven from `/api/v1/health`.
 * (QNT-132 / ADR-015) -- hardcoding here would defeat the purpose: a
 * vendor swap or schedule change must propagate without a frontend
 * deploy.
 *
 * Detached from ISR (QNT-168): this is status metadata, not first-paint-
 * critical content, so the strip self-fetches in the browser with
 * `cache: "no-store"` instead of riding the ticker page's ISR window.
 * Zero ISR Writes from /api/v1/health going forward.
 */

import { useEffect, useState } from "react";

import { API_BASE_URL, type HealthProvenance } from "@/lib/api";

export function ProvenanceStrip() {
  const [provenance, setProvenance] = useState<HealthProvenance | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_BASE_URL}/api/v1/health`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then((res) => (res.ok ? res.json() : null))
      .then((body) => {
        if (body && typeof body === "object" && "provenance" in body) {
          const next = (body as { provenance?: HealthProvenance }).provenance;
          setProvenance(next ?? null);
        }
      })
      .catch(() => {
        // Soft-fail: leave provenance=null so the strip renders em-dashes.
      });
    return () => controller.abort();
  }, []);

  // Soft-fail: if /health is unreachable we still render the row labels with
  // an em-dash so the strip's geometry is stable. Treating this as an error
  // would break the page render for what is essentially a metadata band.
  const sources = provenance?.sources ?? [];
  const jobs = provenance?.jobs;
  const jobsLabel = jobs
    ? `${jobs.runtime} ${jobs.schedule} ${jobs.next_ingest_local}`
    : "—";

  return (
    <footer
      aria-label="Data provenance"
      className="border-t border-zinc-800 bg-zinc-950 px-6 py-1 font-mono text-[10px] uppercase tracking-wider md:py-2"
    >
      {/* <md: one compact line — prefix words hidden and gaps tightened so the
          strip fits a ~390px column on a single row (QNT-249). Desktop keeps
          the labelled two-column layout. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 md:gap-x-6">
        <span className="flex items-baseline gap-2">
          <span className="hidden text-zinc-500 md:inline">Sources</span>
          <span className="text-zinc-300">
            {sources.length > 0 ? sources.join(" · ") : "—"}
          </span>
        </span>
        <span className="flex items-baseline gap-2">
          <span className="hidden text-zinc-500 md:inline">Jobs</span>
          <span className="text-zinc-300">{jobsLabel}</span>
        </span>
      </div>
    </footer>
  );
}
