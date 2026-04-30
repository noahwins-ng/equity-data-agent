/**
 * Quote header — top of the ticker detail middle pane.
 *
 * Server component; reads `/api/v1/quote/{ticker}` once per page render with
 * the route-level revalidate. EOD framing per design v2: the `close <date>`
 * label uses the latest bar's date, NOT a live timestamp — the data is
 * intentionally daily-cadence and a "ticking clock" would lie.
 */

import {
  changeColorClass,
  formatAsOfDate,
  formatCompact,
  formatPct,
  formatPrice,
  formatRatio,
  formatSignedPct,
} from "@/lib/format";
import type { QuoteResponse } from "@/lib/api";

const CAP_TIER = (cap: number | null): string | null => {
  if (cap === null) return null;
  if (cap >= 1_000_000_000_000) return "Mega-cap";
  if (cap >= 200_000_000_000) return "Large-cap";
  if (cap >= 10_000_000_000) return "Mid-cap";
  if (cap >= 2_000_000_000) return "Small-cap";
  return "Micro-cap";
};

export function QuoteHeader({ quote }: { quote: QuoteResponse }) {
  const change =
    quote.price !== null && quote.prev_close !== null && quote.prev_close !== 0
      ? quote.price - quote.prev_close
      : null;
  const changePct =
    change !== null && quote.prev_close !== null && quote.prev_close !== 0
      ? (change / quote.prev_close) * 100
      : null;
  const changeColor = changeColorClass(change);

  const volRatio =
    quote.volume !== null && quote.avg_volume_30d !== null && quote.avg_volume_30d > 0
      ? (quote.volume / quote.avg_volume_30d - 1) * 100
      : null;

  const capTier = CAP_TIER(quote.market_cap);

  return (
    <header
      aria-label={`Quote header for ${quote.ticker}`}
      className="border-b border-zinc-800 bg-zinc-950 px-6 py-1.5"
    >
      <div className="flex items-center gap-4">
        <div className="flex min-w-0 shrink-0 flex-wrap items-baseline gap-x-2 gap-y-1">
          <h1 className="font-mono text-xl font-semibold tracking-tight text-zinc-50">
            {quote.ticker}
          </h1>
          <span className="truncate text-xs text-zinc-300">{quote.name}</span>
          {quote.sector && (
            <span className="rounded border border-zinc-700 px-1 py-px text-[9px] uppercase tracking-wider text-zinc-300">
              {quote.sector}
            </span>
          )}
          {capTier && (
            <span className="rounded border border-zinc-700 px-1 py-px text-[9px] uppercase tracking-wider text-zinc-300">
              {capTier}
            </span>
          )}
        </div>
        <dl className="flex flex-1 items-center justify-center gap-x-6 overflow-hidden">
          <Stat label="Open" value={formatPrice(quote.open)} />
          <Stat
            label="Day range"
            value={
              quote.day_low !== null && quote.day_high !== null
                ? `${formatPrice(quote.day_low)} – ${formatPrice(quote.day_high)}`
                : "—"
            }
          />
          <Stat
            label="Vol"
            value={formatCompact(quote.volume)}
            extra={
              volRatio !== null
                ? `${volRatio >= 0 ? "+" : ""}${formatPct(volRatio, 0)} vs 30d`
                : null
            }
            extraColor={changeColorClass(volRatio)}
          />
          <Stat label="Mkt cap" value={formatCompact(quote.market_cap)} />
          <Stat label="P/E TTM" value={formatRatio(quote.pe_ratio_ttm)} />
        </dl>
        <div className="flex shrink-0 items-baseline gap-2 whitespace-nowrap">
          <span className="font-mono text-2xl font-semibold tabular-nums leading-none text-zinc-50">
            {formatPrice(quote.price)}
          </span>
          <span className={`font-mono text-xs tabular-nums ${changeColor}`}>
            {change !== null ? (change >= 0 ? "+" : "") + formatPrice(change) : "—"}{" "}
            {formatSignedPct(changePct)}
          </span>
          <span className="text-[9px] uppercase tracking-wider text-zinc-500">
            close {formatAsOfDate(quote.as_of)}
          </span>
        </div>
      </div>
    </header>
  );
}

function Stat({
  label,
  value,
  extra,
  extraColor,
}: {
  label: string;
  value: string;
  extra?: string | null;
  extraColor?: string;
}) {
  return (
    <div>
      <dt className="text-[9px] uppercase tracking-wider text-zinc-500">{label}</dt>
      <dd className="font-mono text-xs tabular-nums text-zinc-100">
        {value}
        {extra ? (
          <span className={`ml-1.5 text-[9px] ${extraColor ?? "text-zinc-500"}`}>{extra}</span>
        ) : null}
      </dd>
    </div>
  );
}
