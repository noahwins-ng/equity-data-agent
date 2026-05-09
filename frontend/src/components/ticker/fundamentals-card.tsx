"use client";

/**
 * Fundamentals card — Quarterly / Annual / TTM tabs over
 * `equity_derived.fundamental_summary`.
 *
 * Quarterly tab calibration (QNT-180): P/E, ROE, ROA and FCF yield on the
 * QUARTERLY tab read from the latest TTM row, not from the single-quarter
 * value, because every external dashboard (TradingView, Yahoo, Bloomberg)
 * quotes those four metrics as TTM. Single-quarter ROE / ROA are
 * mathematically valid but make lumpy quarters (NVDA Q4 FY26: 27% vs 76%
 * TTM) look like the wrong number. Annual + TTM tabs are unchanged.
 *
 * Empty-data fallback: when the requested period has no rows we surface a
 * small "no <period> data" line; the caller can hand-pick TTM as a fallback
 * because the rendering logic doesn't reach into the page state.
 */

import { useEffect, useMemo, useState } from "react";

import { apiFetch, type FundamentalRow, type PeriodType } from "@/lib/api";
import { changeColorClass, formatBps, formatCompact, formatPct, formatRatio, formatSignedPct } from "@/lib/format";

const PERIOD_TABS: { id: PeriodType; label: string; short: string }[] = [
  { id: "quarterly", label: "Quarterly", short: "Q" },
  { id: "annual", label: "Annual", short: "A" },
  { id: "ttm", label: "TTM", short: "T" },
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
 * EBITDA margin is only semantically defined on TTM rows: yfinance hands us
 * a single point-in-time TTM EBITDA stamped on every quarterly + annual row,
 * so dividing it by single-quarter revenue produces a meaningless ~4× ratio
 * (AAPL Q4-25 ebitda 159.98B / revenue 143.76B = 111%). The fundamental_summary
 * asset deliberately leaves quarterly + annual ebitda_margin_pct null.
 *
 * On the QUARTERLY + ANNUAL tabs we surface the latest TTM row's value as a
 * fallback so the cell is always informative — the displayed number is the
 * standard way financial dashboards quote "EBITDA margin" (a TTM ratio), and
 * the caller appends a `(TTM)` suffix in render so users know it's not
 * period-aligned. Same pattern as the old roeRoaCell quarterly fallback,
 * but reachable now that `_build_ttm_rows` populates the TTM row.
 */
function pickEbitdaMargin(
  row: FundamentalRow,
  allRows: FundamentalRow[],
): { value: number | null; suffix: string } {
  if (row.period_type === "ttm") {
    return { value: row.ebitda_margin_pct, suffix: "" };
  }
  const ttm = pickLatest(allRows, "ttm");
  return { value: ttm?.ebitda_margin_pct ?? null, suffix: ttm?.ebitda_margin_pct != null ? " (TTM)" : "" };
}

export function FundamentalsCard({
  ticker,
  currentPrice,
}: {
  ticker: string;
  // Latest close from the quote endpoint, used as the P/E numerator on
  // the ANNUAL tab (price / FY-EPS). Quarterly + TTM tabs read the
  // backend-computed `pe_ratio` from the TTM row instead of recomputing.
  currentPrice: number | null;
}) {
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
        <div
          role="tablist"
          aria-label="Period"
          className="flex gap-0.5 text-[10px] 2xl:gap-1"
        >
          {PERIOD_TABS.map((p) => (
            <button
              key={p.id}
              type="button"
              role="tab"
              aria-selected={period === p.id}
              aria-label={p.label}
              onClick={() => setPeriod(p.id)}
              className={
                period === p.id
                  ? "rounded border border-zinc-600 bg-zinc-800 px-1.5 py-0.5 uppercase text-zinc-100"
                  : "rounded border border-transparent px-1.5 py-0.5 uppercase text-zinc-400 hover:bg-zinc-900"
              }
            >
              <span className="2xl:hidden">{p.short}</span>
              <span className="hidden 2xl:inline">{p.label}</span>
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
            {/* P/E label travels with period: Annual = "P/E" (price /
                FY-EPS); Quarterly + TTM = "P/E TTM" (TTM row's pe_ratio).
                peCell returns { label, value }; Row's props match. */}
            <Row {...peCell(display, rows, currentPrice)} />
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
              value={(() => {
                const { value, suffix } = pickEbitdaMargin(display, rows);
                return value === null ? "—" : `${formatPct(value)}${suffix}`;
              })()}
            />
            <Row
              label="Net income"
              value={formatCompact(pickAbsolute(display, "net_income"))}
              extra={formatSignedPct(display.net_income_yoy_pct)}
              extraColor={changeColorClass(display.net_income_yoy_pct)}
              extraLabel="YoY"
            />
            <Row label="EPS" value={formatRatio(display.eps, 2)} />
            {(() => {
              const { yieldValue, yieldSuffix } = pickFcfYield(display, rows);
              // Render order on the QUARTERLY tab (per AC #5):
              //   "<value> <pct> yield (TTM)"
              // The (TTM) marker rides on `extraLabel` so it sits AFTER
              // the "yield" word, not between % and yield. `extraLabel`
              // hides below 2xl (same responsive behavior as the rest of
              // the card) — the percentage value alone is still shown.
              return (
                <Row
                  label="FCF"
                  value={formatCompact(pickAbsolute(display, "fcf"))}
                  extra={yieldValue == null ? "—" : formatPct(yieldValue)}
                  extraColor={changeColorClass(yieldValue)}
                  extraLabel={yieldSuffix ? `yield${yieldSuffix}` : "yield"}
                />
              );
            })()}
            <Row label="Debt / equity" value={formatRatio(display.debt_to_equity, 2)} />
            <Row label="Current ratio" value={formatRatio(display.current_ratio, 2)} />
            <Row label="ROE" value={roeRoaCell(display, rows, "roe")} />
            <Row label="ROA" value={roeRoaCell(display, rows, "roa")} />
          </dl>
        </>
      )}
      </div>
    </section>
  );
}

function peCell(
  row: FundamentalRow,
  allRows: FundamentalRow[],
  price: number | null,
): { label: string; value: string } {
  // Annual: as-reported full-year EPS, label "P/E", price / FY-EPS.
  //   Negative FY-EPS produces a negative P/E (loss-making — valid signal).
  // Quarterly: read pe_ratio from the latest TTM row, label "P/E TTM"
  //   (QNT-180). The earlier price/(Q-EPS×4) annualisation produced
  //   numbers that didn't match TradingView/Yahoo — NVDA Q4-26 was
  //   30.43 vs the canonical 43.55 TTM.
  // TTM: read pe_ratio from the row itself, label "P/E TTM".
  if (row.period_type === "annual") {
    if (price === null || row.eps === null || row.eps === 0) return { label: "P/E", value: "—" };
    return { label: "P/E", value: formatRatio(price / row.eps, 2) };
  }
  const source = row.period_type === "ttm" ? row : pickLatest(allRows, "ttm");
  if (source?.pe_ratio == null) return { label: "P/E TTM", value: "—" };
  return { label: "P/E TTM", value: formatRatio(source.pe_ratio, 2) };
}

function roeRoaCell(
  row: FundamentalRow,
  allRows: FundamentalRow[],
  field: "roe" | "roa",
): string {
  // Annual: full-year NI / equity, as reported.
  // Quarterly: read TTM row's value with " (TTM)" suffix (QNT-180). Single-
  //   quarter ROE/ROA is mathematically valid but lumpy quarters (NVDA Q4
  //   FY26: 27% Q vs 76% TTM) make it look like the wrong number; every
  //   external dashboard quotes TTM. Falls back to "—" if no TTM row.
  // TTM: NI_TTM / equity-at-period-end, with a trailing-4Q caveat.
  if (row.period_type === "ttm") {
    return row[field] == null ? "—" : `${formatPct(row[field])} trailing 4Q`;
  }
  if (row.period_type === "quarterly") {
    const ttm = pickLatest(allRows, "ttm");
    return ttm?.[field] == null ? "—" : `${formatPct(ttm[field])} (TTM)`;
  }
  return row[field] == null ? "—" : formatPct(row[field]);
}

/**
 * FCF yield on QUARTERLY rows reads the latest TTM row's `fcf_yield` and
 * appends a `(TTM)` suffix (QNT-180). Annual + TTM tabs render the row's
 * own value unchanged.
 */
function pickFcfYield(
  row: FundamentalRow,
  allRows: FundamentalRow[],
): { yieldValue: number | null; yieldSuffix: string } {
  if (row.period_type === "quarterly") {
    const ttm = pickLatest(allRows, "ttm");
    return { yieldValue: ttm?.fcf_yield ?? null, yieldSuffix: ttm?.fcf_yield != null ? " (TTM)" : "" };
  }
  return { yieldValue: row.fcf_yield, yieldSuffix: "" };
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
            {/* `YoY` / `yield` suffix hides below 2xl so the row doesn't
                overflow the card on a 14" MacBook. The numeric delta is
                self-describing enough at narrow widths; the label
                returns at 2xl+ where the column has room. */}
            {extraLabel ? (
              <span className="ml-1 hidden text-zinc-500 2xl:inline">{extraLabel}</span>
            ) : null}
          </span>
        ) : null}
      </dd>
    </div>
  );
}
