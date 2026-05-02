/**
 * Quote header — top of the ticker detail middle pane.
 *
 * Server component; reads `/api/v1/quote/{ticker}` once per page render with
 * the route-level revalidate. EOD framing per design v2: the `close <date>`
 * label uses the latest bar's date, NOT a live timestamp — the data is
 * intentionally daily-cadence and a "ticking clock" would lie.
 *
 * The header carries identity (ticker + name + chips) and the latest price.
 * Open / Day range / Volume are NOT duplicated here — the OHLCV strip below
 * the chart already shows them at the bar level. P/E TTM moved into the
 * Fundamentals card (it's a valuation ratio, not a quote attribute). Mkt
 * cap rides as a precise-number suffix on the cap-tier chip rather than as
 * its own stat, because the chip already conveys the same axis (size).
 */

import {
  changeColorClass,
  formatAsOfDate,
  formatCompact,
  formatPrice,
  formatSignedPct,
} from "@/lib/format";
import type { QuoteResponse } from "@/lib/api";
import { TickerLogo } from "@/components/ticker-logo";

const CAP_TIER = (cap: number | null): string | null => {
  if (cap === null) return null;
  if (cap >= 1_000_000_000_000) return "Mega-cap";
  if (cap >= 200_000_000_000) return "Large-cap";
  if (cap >= 10_000_000_000) return "Mid-cap";
  if (cap >= 2_000_000_000) return "Small-cap";
  return "Micro-cap";
};

export function QuoteHeader({
  quote,
  logoUrl,
}: {
  quote: QuoteResponse;
  logoUrl: string | null;
}) {
  const change =
    quote.price !== null && quote.prev_close !== null && quote.prev_close !== 0
      ? quote.price - quote.prev_close
      : null;
  const changePct =
    change !== null && quote.prev_close !== null && quote.prev_close !== 0
      ? (change / quote.prev_close) * 100
      : null;
  const changeColor = changeColorClass(change);

  const capTier = CAP_TIER(quote.market_cap);
  const capValue = formatCompact(quote.market_cap);

  return (
    <header
      aria-label={`Quote header for ${quote.ticker}`}
      className="border-b border-zinc-800 bg-zinc-950 px-6 py-1.5"
    >
      <div className="flex items-center justify-between gap-4">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <TickerLogo ticker={quote.ticker} logoUrl={logoUrl} size={28} />
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
              {/* Cap-tier chip carries both the categorical label and the
                  precise value. "Mega-cap · $5.06T" reads as one atom and
                  saves the row a dedicated Stat for Mkt cap. */}
              {capTier}
              {capValue !== "—" ? (
                <span className="ml-1 normal-case text-zinc-400">· {capValue}</span>
              ) : null}
            </span>
          )}
        </div>
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
