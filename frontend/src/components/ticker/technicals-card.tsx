"use client";

/**
 * Technicals card — RSI / MACD / ADX / ATR / SMA / BB %B / OBV at the latest
 * bar of the selected aggregation (Daily / Weekly / Monthly).
 *
 * Aggregation tabs swap the upstream `/api/v1/indicators?timeframe=...`
 * call. Per ADR-014 §3 client-side toggles bypass the server cache via
 * `cache: "no-store"`.
 */

import { useEffect, useState } from "react";

import { apiFetch, type IndicatorRow, type Timeframe } from "@/lib/api";
import { changeColorClass, formatRatio } from "@/lib/format";

const TIMEFRAMES: { id: Timeframe; label: string }[] = [
  { id: "daily", label: "Daily" },
  { id: "weekly", label: "Weekly" },
  { id: "monthly", label: "Monthly" },
];

type Decoration = { label: string; tone: "bull" | "bear" | "neutral" };

function rsiTag(rsi: number | null): Decoration | null {
  if (rsi === null) return null;
  if (rsi >= 80) return { label: "extended", tone: "bull" };
  if (rsi >= 70) return { label: "overbought", tone: "bull" };
  if (rsi <= 20) return { label: "extended", tone: "bear" };
  if (rsi <= 30) return { label: "oversold", tone: "bear" };
  return { label: "neutral", tone: "neutral" };
}

function adxTag(adx: number | null): Decoration | null {
  if (adx === null) return null;
  if (adx >= 40) return { label: "very strong", tone: "bull" };
  if (adx >= 25) return { label: "strong", tone: "bull" };
  if (adx >= 20) return { label: "elev", tone: "neutral" };
  return { label: "weak", tone: "neutral" };
}

function bbPctBTag(pctB: number | null): Decoration | null {
  if (pctB === null) return null;
  if (pctB >= 1.0) return { label: "upper-band", tone: "bull" };
  if (pctB <= 0.0) return { label: "lower-band", tone: "bear" };
  if (pctB >= 0.8) return { label: "upper", tone: "bull" };
  if (pctB <= 0.2) return { label: "lower", tone: "bear" };
  return null;
}

function macdTag(row: IndicatorRow): Decoration | null {
  if (row.macd === null || row.macd_signal === null) return null;
  if (row.macd_bullish_cross === 1) return { label: "bull cross", tone: "bull" };
  return row.macd > row.macd_signal
    ? { label: "positive", tone: "bull" }
    : { label: "negative", tone: "bear" };
}

function smaPctAbove(price: number | null, sma: number | null): number | null {
  if (price === null || sma === null || sma === 0) return null;
  return ((price - sma) / sma) * 100;
}

function toneClass(tone: Decoration["tone"]): string {
  if (tone === "bull") return "text-emerald-400";
  if (tone === "bear") return "text-red-400";
  return "text-zinc-400";
}

function ObvTrendLabel({ rows }: { rows: IndicatorRow[] }) {
  // OBV trend = sign of the slope over the last 20 bars (rough but stable).
  const tail = rows.slice(-20).filter((r) => r.obv !== null);
  if (tail.length < 2) return <span className="text-zinc-500">—</span>;
  const first = tail[0].obv as number;
  const last = tail[tail.length - 1].obv as number;
  const direction = last - first;
  if (direction === 0) return <span className="text-zinc-400">flat</span>;
  return direction > 0 ? (
    <span className="text-emerald-400">rising</span>
  ) : (
    <span className="text-red-400">falling</span>
  );
}

function Tag({ tag }: { tag: Decoration | null }) {
  if (!tag) return null;
  // Pill rendering matches the v2 reference — colored text inside a thin
  // border so the chip is visually distinct from the numeric value next to
  // it (no more "65.4 NEUTRAL" running together).
  return (
    <span
      className={`rounded border px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider ${toneClass(tag.tone)} ${borderToneClass(tag.tone)}`}
    >
      {tag.label}
    </span>
  );
}

function borderToneClass(tone: Decoration["tone"]): string {
  if (tone === "bull") return "border-emerald-700/60";
  if (tone === "bear") return "border-red-700/60";
  return "border-zinc-700";
}

export function TechnicalsCard({ ticker }: { ticker: string }) {
  const [timeframe, setTimeframe] = useState<Timeframe>("daily");
  // Co-locate fetched data with its dep tuple so loading/error are derived
  // in render, not set synchronously inside an effect (React 19 lint).
  const [loaded, setLoaded] = useState<{
    key: string | null;
    rows: IndicatorRow[];
    latestPrice: number | null;
    error: string | null;
  }>({ key: null, rows: [], latestPrice: null, error: null });

  const requestKey = `${ticker}/${timeframe}`;

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      apiFetch<IndicatorRow[]>(`/api/v1/indicators/${ticker}?timeframe=${timeframe}`, {
        cache: "no-store",
      }),
      // Pull the latest close from the matching OHLCV timeframe so the SMA
      // "% above" math is exact instead of approximated from the SMA itself.
      apiFetch<{ close: number }[]>(`/api/v1/ohlcv/${ticker}?timeframe=${timeframe}`, {
        cache: "no-store",
      }),
    ])
      .then(([rows, ohlcv]) => {
        if (cancelled) return;
        const last = ohlcv.length > 0 ? ohlcv[ohlcv.length - 1].close : null;
        setLoaded({ key: requestKey, rows, latestPrice: last, error: null });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : "unknown error";
          setLoaded({ key: requestKey, rows: [], latestPrice: null, error: msg });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [ticker, timeframe, requestKey]);

  const isCurrent = loaded.key === requestKey;
  const rows = isCurrent ? loaded.rows : [];
  const latestPrice = isCurrent ? loaded.latestPrice : null;
  const error = isCurrent ? loaded.error : null;
  const loading = !isCurrent;
  const latest: IndicatorRow | null = rows.length > 0 ? rows[rows.length - 1] : null;

  return (
    <section
      aria-label="Technical indicators"
      className="flex min-h-0 flex-col rounded border border-zinc-800 bg-zinc-950"
    >
      <div className="flex shrink-0 items-baseline justify-between border-b border-zinc-800 px-4 py-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-200">
          Technicals
        </h2>
        <div role="tablist" aria-label="Aggregation" className="flex gap-1 text-[10px]">
          {TIMEFRAMES.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={timeframe === t.id}
              onClick={() => setTimeframe(t.id)}
              className={
                timeframe === t.id
                  ? "rounded border border-zinc-600 bg-zinc-800 px-1.5 py-0.5 uppercase text-zinc-100"
                  : "rounded border border-transparent px-1.5 py-0.5 uppercase text-zinc-400 hover:bg-zinc-900"
              }
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      <div className="min-h-0 flex-1 px-4 py-1">
      {error ? (
        <p className="text-sm text-red-400" data-testid="technicals-error">
          Indicators unavailable. <span className="text-zinc-500">{error}</span>
        </p>
      ) : loading ? (
        <p className="text-sm text-zinc-500">Loading…</p>
      ) : !latest ? (
        <p className="text-sm text-zinc-500">No data.</p>
      ) : (
        <dl className="divide-y divide-zinc-800/60 text-sm">
          <Row label="RSI(14)" value={formatRatio(latest.rsi_14, 1)} tag={rsiTag(latest.rsi_14)} />
          <Row
            label="MACD(12,26,9)"
            value={
              latest.macd !== null
                ? `${formatRatio(latest.macd, 2)} / ${formatRatio(latest.macd_signal, 2)}`
                : "—"
            }
            tag={macdTag(latest)}
          />
          <Row label="ADX(14)" value={formatRatio(latest.adx_14, 1)} tag={adxTag(latest.adx_14)} />
          <Row label="ATR(14)" value={formatRatio(latest.atr_14, 2)} />
          <Row
            label="SMA 20"
            value={formatRatio(latest.sma_20, 2)}
            extra={pctAboveLabel(smaPctAbove(latestPrice, latest.sma_20))}
            extraColor={changeColorClass(smaPctAbove(latestPrice, latest.sma_20))}
          />
          <Row
            label="SMA 50"
            value={formatRatio(latest.sma_50, 2)}
            extra={pctAboveLabel(smaPctAbove(latestPrice, latest.sma_50))}
            extraColor={changeColorClass(smaPctAbove(latestPrice, latest.sma_50))}
          />
          <Row
            label="SMA 200"
            value={formatRatio(latest.sma_200, 2)}
            extra={pctAboveLabel(smaPctAbove(latestPrice, latest.sma_200))}
            extraColor={changeColorClass(smaPctAbove(latestPrice, latest.sma_200))}
          />
          <Row
            label="Bollinger %B"
            value={formatRatio(latest.bb_pct_b, 2)}
            tag={bbPctBTag(latest.bb_pct_b)}
          />
          <Row label="OBV trend" valueNode={<ObvTrendLabel rows={rows} />} />
        </dl>
      )}
      </div>
    </section>
  );
}

function pctAboveLabel(pct: number | null): string | null {
  if (pct === null) return null;
  const sign = pct >= 0 ? "+" : "";
  return `${sign}${pct.toFixed(1)}%`;
}

function Row({
  label,
  value,
  valueNode,
  tag,
  extra,
  extraColor,
}: {
  label: string;
  value?: string;
  valueNode?: React.ReactNode;
  tag?: Decoration | null;
  extra?: string | null;
  extraColor?: string;
}) {
  // Three-column layout: label / numeric value / decoration chip. Each column
  // has its own track so values stay right-aligned regardless of label width
  // and chips don't bump into the value text (the v2-final reference: clean
  // gutters, monospaced numerics, pill-bordered chips).
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-x-3 py-1">
      <dt className="text-[11px] uppercase tracking-wider text-zinc-400">{label}</dt>
      <dd className="text-right font-mono text-sm tabular-nums text-zinc-50">
        {valueNode ?? value}
        {extra ? (
          <span className={`ml-1.5 text-[11px] ${extraColor ?? "text-zinc-500"}`}>{extra}</span>
        ) : null}
      </dd>
      <div className="min-w-0">
        <Tag tag={tag ?? null} />
      </div>
    </div>
  );
}
