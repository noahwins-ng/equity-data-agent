/**
 * Provenance strip — bottom of the ticker detail middle pane.
 *
 * Two rows: SOURCES and JOBS, both data-driven from `/api/v1/health`
 * (QNT-132, ADR-015 revision history). Hardcoding the values here would
 * defeat the purpose — a vendor swap or schedule change must propagate
 * without a frontend deploy.
 */

import type { HealthProvenance } from "@/lib/api";

export function ProvenanceStrip({
  provenance,
}: {
  provenance: HealthProvenance | null;
}) {
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
      className="border-t border-zinc-800 bg-zinc-950 px-6 py-2 font-mono text-[10px] uppercase tracking-wider"
    >
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1">
        <span className="flex items-baseline gap-2">
          <span className="text-zinc-500">Sources</span>
          <span className="text-zinc-300">
            {sources.length > 0 ? sources.join(" · ") : "—"}
          </span>
        </span>
        <span className="flex items-baseline gap-2">
          <span className="text-zinc-500">Jobs</span>
          <span className="text-zinc-300">{jobsLabel}</span>
        </span>
      </div>
    </footer>
  );
}
