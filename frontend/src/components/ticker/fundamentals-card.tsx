"use client";

/**
 * Fundamentals card — Quarterly / Annual / TTM tabs over
 * `equity_derived.fundamental_summary`.
 *
 * ROE / ROA framing:
 *   - On Quarterly the API does not store a point-in-time ROE/ROA (those need
 *     a trailing-4Q income figure to be meaningful), so we render the literal
 *     `N/A point-in-time` per design v2.
 *   - On TTM we render the value with the trailing-4Q caveat.
 *   - On Annual we render the as-reported value (the QNT-134 contract).
 *
 * Empty-data fallback (AC #9): when the requested period has no rows we
 * surface a small "no <period> data" line; the caller can hand-pick TTM as a
 * fallback because the rendering logic doesn't reach into the page state.
 */

import { useEffect, useMemo, useState } from "react";

import { apiFetch, type FundamentalRow, type PeriodType } from "@/lib/api";
import { changeColorClass, formatBps, formatCompact, formatPct, formatRatio, formatSignedPct } from "@/lib/format";

const PERIOD_TABS: { id: PeriodType; label: string }[] = [
  { id: "quarterly", label: "Quarterly" },
  { id: "annual", label: "Annual" },
  { id: "ttm", label: "TTM" },
];

function pickLatest(rows: FundamentalRow[], period: PeriodType): FundamentalRow | null {
  const matching = rows.filter((r) => r.period_type === period);
  if (matching.length === 0) return null;
  // API already orders most-recent-first, but sort defensively.
  return [...matching].sort((a, b) => b.period_end.localeCompare(a.period_end))[0];
}

/**
 * Absolute Revenue / Net income / FCF lookup that handles the period-type
 * branching: TTM rows use the rolling-4Q aggregate columns; quarterly + annual
 * rows use the raw equity_raw.fundamentals columns surfaced by the LEFT JOIN.
 */
function pickAbsolute(row: FundamentalRow, kind: "revenue" | "net_income" | "fcf"): number | null {
  if (row.period_type === "ttm") {
    if (kind === "revenue") return row.revenue_ttm;
    if (kind === "net_income") return row.net_income_ttm;
    return row.fcf_ttm;
  }
  if (kind === "revenue") return row.revenue;
  if (kind === "net_income") return row.net_income;
  return row.free_cash_flow;
}

/**
 * EBITDA margin: stored on TTM rows directly; for quarterly + annual the
 * fundamental_summary asset deliberately leaves it null (avoiding a
 * single-quarter EBITDA / single-quarter revenue ratio that would be
 * meaningless), so derive it from the raw EBITDA / revenue here.
 */
function pickEbitdaMargin(row: FundamentalRow): number | null {
  if (row.period_type === "ttm") return row.ebitda_margin_pct;
  if (row.ebitda === null || row.revenue === null || row.revenue === 0) return null;
  return (row.ebitda / row.revenue) * 100;
}

export function FundamentalsCard({ ticker }: { ticker: string }) {
  const [period, setPeriod] = useState<PeriodType>("annual");
  // Co-locate data with the ticker it was fetched for; derive loading/error
  // in render to avoid setState-synchronously-inside-effect (React 19 lint).
  const [loaded, setLoaded] = useState<{
    ticker: string | null;
    rows: FundamentalRow[];
    error: string | null;
  }>({ ticker: null, rows: [], error: null });

  useEffect(() => {
    let cancelled = false;
    apiFetch<FundamentalRow[]>(`/api/v1/fundamentals/${ticker}`, { cache: "no-store" })
      .then((r) => {
        if (!cancelled) setLoaded({ ticker, rows: r, error: null });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : "unknown error";
          setLoaded({ ticker, rows: [], error: msg });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [ticker]);

  const isCurrent = loaded.ticker === ticker;
  const rows = useMemo(
    () => (isCurrent ? loaded.rows : []),
    [isCurrent, loaded.rows],
  );
  const error = isCurrent ? loaded.error : null;
  const loading = !isCurrent;

  const row = useMemo(() => pickLatest(rows, period), [rows, period]);
  // AC #9 — empty-state fallback: TTM rolls up from quarterly, so a missing
  // TTM row falls back to the latest annual; missing annual falls back to TTM.
  const fallbackRow = useMemo(() => {
    if (row) return null;
    if (period === "ttm") return pickLatest(rows, "annual");
    if (period === "annual") return pickLatest(rows, "ttm");
    return null;
  }, [rows, period, row]);

  const display = row ?? fallbackRow;

  return (
    <section
      aria-label="Fundamentals"
      className="flex min-h-0 flex-col rounded border border-zinc-800 bg-zinc-950"
    >
      <div className="flex shrink-0 items-baseline justify-between border-b border-zinc-800 px-4 py-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-200">
          Fundamentals
        </h2>
        <div role="tablist" aria-label="Period" className="flex gap-1 text-[10px]">
          {PERIOD_TABS.map((p) => (
            <button
              key={p.id}
              type="button"
              role="tab"
              aria-selected={period === p.id}
              onClick={() => setPeriod(p.id)}
              className={
                period === p.id
                  ? "rounded border border-zinc-600 bg-zinc-800 px-1.5 py-0.5 uppercase text-zinc-100"
                  : "rounded border border-transparent px-1.5 py-0.5 uppercase text-zinc-400 hover:bg-zinc-900"
              }
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-1">
      {error ? (
        <p className="text-sm text-red-400" data-testid="fundamentals-error">
          Fundamentals unavailable. <span className="text-zinc-500">{error}</span>
        </p>
      ) : loading ? (
        <p className="text-sm text-zinc-500">Loading…</p>
      ) : !display ? (
        <p className="text-sm text-zinc-500">No fundamentals data.</p>
      ) : (
        <>
          {!row && fallbackRow ? (
            <p className="mb-3 text-[10px] uppercase tracking-wider text-amber-400">
              No {period} data — showing {fallbackRow.period_type}.
            </p>
          ) : (
            <p className="mb-2 text-[10px] uppercase tracking-wider text-zinc-500">
              As of {display.period_end} · {display.period_type}
            </p>
          )}
          <dl className="divide-y divide-zinc-800/60 text-sm">
            <Row
              label="Revenue"
              value={formatCompact(pickAbsolute(display, "revenue"))}
              extra={formatSignedPct(display.revenue_yoy_pct)}
              extraColor={changeColorClass(display.revenue_yoy_pct)}
              extraLabel="YoY"
            />
            <Row
              label="Gross margin"
              value={formatPct(display.gross_margin_pct)}
              extra={formatBps(display.gross_margin_bps_yoy)}
              extraColor={changeColorClass(display.gross_margin_bps_yoy)}
              extraLabel="YoY"
            />
            <Row
              label="EBITDA margin"
              value={formatPct(pickEbitdaMargin(display))}
            />
            <Row
              label="Net income"
              value={formatCompact(pickAbsolute(display, "net_income"))}
              extra={formatSignedPct(display.net_income_yoy_pct)}
              extraColor={changeColorClass(display.net_income_yoy_pct)}
              extraLabel="YoY"
            />
            <Row label="EPS" value={formatRatio(display.eps, 2)} />
            <Row
              label="FCF"
              value={formatCompact(pickAbsolute(display, "fcf"))}
              extra={formatPct(display.fcf_yield)}
              extraColor={changeColorClass(display.fcf_yield)}
              extraLabel="yield"
            />
            <Row label="Debt / equity" value={formatRatio(display.debt_to_equity, 2)} />
            <Row label="Current ratio" value={formatRatio(display.current_ratio, 2)} />
            <Row label="ROE" value={roeRoaCell(display, "roe", rows)} />
            <Row label="ROA" value={roeRoaCell(display, "roa", rows)} />
          </dl>
        </>
      )}
      </div>
    </section>
  );
}

function roeRoaCell(
  row: FundamentalRow,
  field: "roe" | "roa",
  allRows: FundamentalRow[],
): string {
  // Annual: as-reported, no caveat needed.
  if (row.period_type === "annual") {
    return row[field] === null ? "—" : formatPct(row[field]);
  }
  // TTM: API now computes from BS values when the asset left them null,
  // so the row's own value is what we want.
  if (row.period_type === "ttm") {
    return row[field] === null ? "—" : `${formatPct(row[field])} trailing 4Q`;
  }
  // Quarterly: a single quarter's NI ÷ equity is meaningless. Fall back to
  // the latest TTM row's value with a `(TTM)` suffix — that's what
  // most financial dashboards surface for "Q3 ROE" anyway.
  const ttm = pickLatest(allRows, "ttm");
  if (!ttm || ttm[field] === null) return "—";
  return `${formatPct(ttm[field])} (TTM)`;
}

function Row({
  label,
  value,
  extra,
  extraColor,
  extraLabel,
}: {
  label: string;
  value: string;
  extra?: string;
  extraColor?: string;
  extraLabel?: string;
}) {
  // Two-column track: label on the left, numeric value + a trailing
  // delta-pill on the right. Symmetric with the technicals card so the
  // three middle-pane cards read at the same rhythm.
  //
  // `1fr` (= `minmax(auto, 1fr)`) keeps the label column at min-content
  // width as a floor, so labels like "EBITDA MARGIN" don't ellipsis-clip
  // at narrow viewports.
  return (
    <div className="grid grid-cols-[1fr_auto] items-center gap-x-3 py-1">
      <dt className="whitespace-nowrap text-[11px] uppercase tracking-wider text-zinc-400">
        {label}
      </dt>
      <dd className="whitespace-nowrap text-right font-mono text-sm tabular-nums text-zinc-50">
        {value}
        {extra && extra !== "—" ? (
          <span className={`ml-1.5 text-[11px] ${extraColor ?? "text-zinc-500"}`}>
            {extra}
            {extraLabel ? <span className="ml-1 text-zinc-500">{extraLabel}</span> : null}
          </span>
        ) : null}
      </dd>
    </div>
  );
}
