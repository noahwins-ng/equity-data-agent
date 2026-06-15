// ─── Tool-call row ────────────────────────────────────────────────────────

import type { ToolRow } from "./types";

export function ToolCallRow({ row }: { row: ToolRow }) {
  const args = Object.entries(row.args)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(" · ");
  const result = row.result;
  const status = result ? (result.ok ? "✓" : "✗") : "…";
  const statusColor = result
    ? result.ok
      ? "text-emerald-400"
      : "text-red-400"
    : "text-zinc-500";

  return (
    <div className="flex items-baseline justify-between gap-2 font-mono text-[11px] text-zinc-400">
      <div className="min-w-0 flex-1 truncate">
        <span className="text-zinc-500">→ </span>
        <span className="text-zinc-200">{row.label}</span>
        {args && <span className="text-zinc-500"> · {args}</span>}
        {result && <span className="text-zinc-500"> · {result.summary}</span>}
      </div>
      <div className="flex shrink-0 items-baseline gap-1 tabular-nums">
        {result && (
          <span className="text-zinc-500">{result.latency_ms}ms</span>
        )}
        <span className={statusColor}>{status}</span>
      </div>
    </div>
  );
}
