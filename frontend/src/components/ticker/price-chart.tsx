"use client";

/**
 * Candlestick + volume chart for the ticker detail page.
 *
 * Client component — TradingView Lightweight Charts is a DOM-bound canvas
 * library and cannot run inside the RSC payload. The wrapper fetches OHLCV +
 * indicators + (optional) SPY benchmark on the client when the user toggles
 * the date range, indicator chips, or benchmark overlay; per ADR-014 §3
 * those toggles bypass the server cache via `cache: "no-store"`.
 *
 * Split-continuity: the candlestick is back-adjusted using `adj_close/close`
 * so a 10-for-1 split (NVDA, 2024) doesn't appear as a 90% drop. This is the
 * standard back-adjustment math TradingView itself applies — yfinance
 * `auto_adjust=False` keeps raw OHLC + an adj_close column for exactly this
 * purpose.
 */

import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { apiFetch, type IndicatorRow, type OhlcvRow, type Timeframe } from "@/lib/api";

// ─── Date-range presets ────────────────────────────────────────────────────
//
// `null` days → MAX (use the full series). Order is the rendering order on
// the chip strip.

const DATE_RANGES = [
  { id: "1M", label: "1M", days: 31 },
  { id: "3M", label: "3M", days: 92 },
  { id: "6M", label: "6M", days: 184 },
  { id: "YTD", label: "YTD", days: -1 },
  { id: "1Y", label: "1Y", days: 365 },
  { id: "5Y", label: "5Y", days: 365 * 5 },
  { id: "MAX", label: "MAX", days: null },
] as const;

type RangeId = (typeof DATE_RANGES)[number]["id"];

// ─── Indicator overlays ────────────────────────────────────────────────────

type OverlayId = "SMA" | "BB" | "RSI" | "MACD" | "ATR" | "OBV";

const OVERLAY_LABELS: Record<OverlayId, string> = {
  SMA: "SMA 20/50/200",
  BB: "Bollinger Bands",
  RSI: "RSI 14",
  MACD: "MACD 12/26/9",
  ATR: "ATR 14",
  OBV: "OBV",
};

const OVERLAY_TONE: Record<OverlayId, string> = {
  SMA: "#fbbf24", // amber-400
  BB: "#a78bfa", // violet-400
  RSI: "#22d3ee", // cyan-400
  MACD: "#60a5fa", // blue-400 — MACD (fast) line; signal (slow) uses red (see indicator effect)
  ATR: "#f472b6", // pink-400
  OBV: "#94a3b8", // slate-400
};

// ─── Helpers ────────────────────────────────────────────────────────────────

function isoToTime(iso: string): UTCTimestamp {
  // Lightweight Charts accepts a UTC seconds timestamp for daily bars; the ISO
  // string variant is fine too but coercing once here keeps the comparator
  // happy when we mix series with different lengths.
  return Math.floor(Date.parse(`${iso}T00:00:00Z`) / 1000) as UTCTimestamp;
}

function filterByRange<T extends { time: UTCTimestamp }>(rows: T[], range: RangeId): T[] {
  const preset = DATE_RANGES.find((r) => r.id === range);
  if (!preset || preset.days === null) return rows;

  if (preset.days === -1) {
    // YTD — slice from Jan 1 of the latest bar's year.
    if (rows.length === 0) return rows;
    const lastIso = new Date(rows[rows.length - 1].time * 1000);
    const startSec = Math.floor(
      Date.UTC(lastIso.getUTCFullYear(), 0, 1) / 1000,
    ) as UTCTimestamp;
    return rows.filter((r) => r.time >= startSec);
  }

  if (rows.length === 0) return rows;
  const lastSec = rows[rows.length - 1].time;
  const cutoff = (lastSec - preset.days * 86400) as UTCTimestamp;
  return rows.filter((r) => r.time >= cutoff);
}

function backAdjusted(ohlcv: OhlcvRow[]): CandlestickData<UTCTimestamp>[] {
  return ohlcv
    .filter((r) => r.close > 0)
    .map((r) => {
      const ratio = r.close === 0 ? 1 : r.adj_close / r.close;
      return {
        time: isoToTime(r.time),
        open: r.open * ratio,
        high: r.high * ratio,
        low: r.low * ratio,
        close: r.adj_close,
      };
    });
}

function volumeBars(
  ohlcv: OhlcvRow[],
  candles: CandlestickData<UTCTimestamp>[],
): HistogramData<UTCTimestamp>[] {
  const closeByTime = new Map<UTCTimestamp, { close: number; open: number }>();
  for (const c of candles) {
    closeByTime.set(c.time, { close: c.close, open: c.open });
  }
  return ohlcv.map((r) => {
    const t = isoToTime(r.time);
    const c = closeByTime.get(t);
    const up = c ? c.close >= c.open : true;
    return {
      time: t,
      value: r.volume,
      color: up ? "rgba(34, 197, 94, 0.45)" : "rgba(239, 68, 68, 0.45)",
    };
  });
}

function indicatorLine(
  rows: IndicatorRow[],
  field: keyof IndicatorRow,
): LineData<UTCTimestamp>[] {
  const series: LineData<UTCTimestamp>[] = [];
  for (const r of rows) {
    const v = r[field];
    if (typeof v === "number" && !Number.isNaN(v)) {
      series.push({ time: isoToTime(r.time), value: v });
    }
  }
  return series;
}

function spyOverlayLine(
  spy: OhlcvRow[],
  base: OhlcvRow[],
): LineData<UTCTimestamp>[] {
  // Normalize SPY to the symbol's first visible price so a 0% baseline
  // anchors at the same point — this is the "compare-as-percent-from-start"
  // convention every chart vendor uses for benchmark overlays.
  if (spy.length === 0 || base.length === 0) return [];
  const baseStart = base[0].adj_close || base[0].close;
  const spyStart = spy[0].adj_close || spy[0].close;
  if (baseStart === 0 || spyStart === 0) return [];
  return spy.map((r) => ({
    time: isoToTime(r.time),
    value: ((r.adj_close || r.close) / spyStart) * baseStart,
  }));
}

// ─── Component ──────────────────────────────────────────────────────────────

export function PriceChart({ ticker }: { ticker: string }) {
  // 2 years of daily bars are backfilled (assets/ohlcv_raw.py), so MAX is
  // genuinely "everything we have" — saves the user clicking through the
  // preset chain to see the full window.
  const [range, setRange] = useState<RangeId>("MAX");
  const [barInterval, setBarInterval] = useState<Timeframe>("daily");
  // Default to a clean candlestick-only view. Each overlay is opt-in so the
  // chart matches the v2 design reference at first paint; the chips above the
  // chart make the interactivity discoverable. Per-bar indicator readouts live
  // in the technicals card, which is the canonical numeric surface.
  const [overlays, setOverlays] = useState<Record<OverlayId, boolean>>({
    SMA: false,
    BB: false,
    RSI: false,
    MACD: false,
    ATR: false,
    OBV: false,
  });
  const [logScale, setLogScale] = useState(false);
  const [showSpy, setShowSpy] = useState(false);

  // Loaded state co-locates the data with the dep-tuple it was fetched for —
  // lets us derive "loading" / "error" without setState-during-effect (React 19
  // disallows; flagged by react-hooks/set-state-in-effect). The key bundles
  // both ticker and bar interval so a tab switch (D/W/M) is treated as a fresh
  // request and stale data doesn't render against the new axis.
  const [loaded, setLoaded] = useState<{
    key: string | null;
    ohlcv: OhlcvRow[];
    indicators: IndicatorRow[];
    error: string | null;
  }>({ key: null, ohlcv: [], indicators: [], error: null });
  const [spy, setSpy] = useState<{ tf: Timeframe | null; data: OhlcvRow[] }>({
    tf: null,
    data: [],
  });
  const requestKey = `${ticker}/${barInterval}`;
  // Crosshair-driven OHLC + volume readout. Stays null when the cursor
  // leaves the chart; the readout component then falls back to the latest
  // visible bar so there's always something to render.
  const [hovered, setHovered] = useState<UTCTimestamp | null>(null);
  const onCrosshairMove = useCallback((time: UTCTimestamp | null) => {
    setHovered(time);
  }, []);

  // Fetch base OHLCV + indicators on ticker / interval change.
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      apiFetch<OhlcvRow[]>(`/api/v1/ohlcv/${ticker}?timeframe=${barInterval}`, {
        cache: "no-store",
      }),
      apiFetch<IndicatorRow[]>(`/api/v1/indicators/${ticker}?timeframe=${barInterval}`, {
        cache: "no-store",
      }),
    ])
      .then(([o, ind]) => {
        if (!cancelled) {
          setLoaded({ key: requestKey, ohlcv: o, indicators: ind, error: null });
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : "unknown error";
          setLoaded({ key: requestKey, ohlcv: [], indicators: [], error: msg });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [ticker, barInterval, requestKey]);

  // Stale data from a previous ticker / interval is treated as no-data while
  // the new request is in flight, so the chart doesn't flash the wrong axis.
  // Memoised so downstream useMemos see stable references when the dep tuple
  // hasn't changed.
  const isCurrent = loaded.key === requestKey;
  const ohlcv = useMemo(
    () => (isCurrent ? loaded.ohlcv : []),
    [isCurrent, loaded.ohlcv],
  );
  const indicators = useMemo(
    () => (isCurrent ? loaded.indicators : []),
    [isCurrent, loaded.indicators],
  );
  const loadError = isCurrent ? loaded.error : null;

  // SPY overlay is fetched lazily when toggled on. Cache is keyed by interval
  // so toggling D/W/M while SPY is on triggers a refetch but a SPY off→on
  // toggle within the same interval reuses the cached data.
  useEffect(() => {
    if (!showSpy) return;
    if (spy.tf === barInterval && spy.data.length > 0) return;
    let cancelled = false;
    apiFetch<OhlcvRow[]>(`/api/v1/ohlcv/SPY?timeframe=${barInterval}`, {
      cache: "no-store",
    })
      .then((r) => {
        if (!cancelled) setSpy({ tf: barInterval, data: r });
      })
      .catch(() => {
        // Soft-fail: a missing SPY just leaves the toggle on with no overlay.
      });
    return () => {
      cancelled = true;
    };
  }, [showSpy, barInterval, spy.tf, spy.data.length]);

  const candles = useMemo(() => backAdjusted(ohlcv), [ohlcv]);
  const volume = useMemo(() => volumeBars(ohlcv, candles), [ohlcv, candles]);
  const visibleCandles = useMemo(() => filterByRange(candles, range), [candles, range]);
  const visibleVolume = useMemo(() => filterByRange(volume, range), [volume, range]);
  const visibleIndicators = useMemo(() => {
    return indicators
      .map((r) => ({ ...r, _t: isoToTime(r.time) }))
      .filter((r) => {
        if (visibleCandles.length === 0) return true;
        return r._t >= visibleCandles[0].time && r._t <= visibleCandles[visibleCandles.length - 1].time;
      });
  }, [indicators, visibleCandles]);

  const visibleSpy = useMemo(() => {
    if (!showSpy || spy.tf !== barInterval || spy.data.length === 0) return [];
    const first = visibleCandles[0]?.time;
    const last = visibleCandles[visibleCandles.length - 1]?.time;
    if (first === undefined || last === undefined) return [];
    const sliced = spy.data.filter((r) => {
      const t = isoToTime(r.time);
      return t >= first && t <= last;
    });
    const ohlcvBase = ohlcv.filter((r) => {
      const t = isoToTime(r.time);
      return t >= first && t <= last;
    });
    return spyOverlayLine(sliced, ohlcvBase);
  }, [showSpy, spy, barInterval, ohlcv, visibleCandles]);

  return (
    <section
      aria-label="Price chart"
      className="border-b border-zinc-800 bg-zinc-950 px-6 py-2"
    >
      <ChartToolbar
        range={range}
        onRangeChange={setRange}
        barInterval={barInterval}
        onBarIntervalChange={setBarInterval}
        overlays={overlays}
        onToggleOverlay={(id) => setOverlays((prev) => ({ ...prev, [id]: !prev[id] }))}
        showSpy={showSpy}
        onToggleSpy={() => setShowSpy((s) => !s)}
        logScale={logScale}
        onToggleLog={() => setLogScale((l) => !l)}
      />
      <OhlcReadout
        candles={visibleCandles}
        volume={visibleVolume}
        indicators={visibleIndicators}
        overlays={overlays}
        hovered={hovered}
      />
      {loadError ? (
        <p className="my-12 text-center text-sm text-red-400" data-testid="chart-error">
          Chart unavailable. <span className="text-zinc-500">{loadError}</span>
        </p>
      ) : (
        <ChartCanvas
          candles={visibleCandles}
          volume={visibleVolume}
          indicators={visibleIndicators}
          overlays={overlays}
          spyLine={visibleSpy}
          logScale={logScale}
          onCrosshairMove={onCrosshairMove}
        />
      )}
    </section>
  );
}

function ChartToolbar({
  range,
  onRangeChange,
  barInterval,
  onBarIntervalChange,
  overlays,
  onToggleOverlay,
  showSpy,
  onToggleSpy,
  logScale,
  onToggleLog,
}: {
  range: RangeId;
  onRangeChange: (r: RangeId) => void;
  barInterval: Timeframe;
  onBarIntervalChange: (tf: Timeframe) => void;
  overlays: Record<OverlayId, boolean>;
  onToggleOverlay: (id: OverlayId) => void;
  showSpy: boolean;
  onToggleSpy: () => void;
  logScale: boolean;
  onToggleLog: () => void;
}) {
  const INTERVALS: { id: Timeframe; label: string }[] = [
    { id: "daily", label: "D" },
    { id: "weekly", label: "W" },
    { id: "monthly", label: "M" },
  ];
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 pb-2 text-[11px] uppercase tracking-wider">
      <div className="flex items-center gap-3">
        <div role="tablist" aria-label="Date range" className="flex gap-1">
          {DATE_RANGES.map((r) => (
            <button
              key={r.id}
              type="button"
              role="tab"
              aria-selected={range === r.id}
              onClick={() => onRangeChange(r.id)}
              className={
                range === r.id
                  ? "rounded border border-zinc-600 bg-zinc-800 px-2 py-0.5 text-zinc-100"
                  : "rounded border border-transparent px-2 py-0.5 text-zinc-400 hover:bg-zinc-900"
              }
            >
              {r.label}
            </button>
          ))}
        </div>
        <div role="tablist" aria-label="Bar interval" className="flex gap-1 border-l border-zinc-800 pl-3">
          {INTERVALS.map((iv) => (
            <button
              key={iv.id}
              type="button"
              role="tab"
              aria-selected={barInterval === iv.id}
              onClick={() => onBarIntervalChange(iv.id)}
              className={
                barInterval === iv.id
                  ? "rounded border border-zinc-600 bg-zinc-800 px-2 py-0.5 text-zinc-100"
                  : "rounded border border-transparent px-2 py-0.5 text-zinc-400 hover:bg-zinc-900"
              }
            >
              {iv.label}
            </button>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap gap-1">
        {(Object.keys(OVERLAY_LABELS) as OverlayId[]).map((id) => (
          <button
            key={id}
            type="button"
            aria-pressed={overlays[id]}
            onClick={() => onToggleOverlay(id)}
            title={OVERLAY_LABELS[id]}
            className={
              overlays[id]
                ? "rounded border px-1.5 py-0.5 text-zinc-100"
                : "rounded border border-zinc-700 px-1.5 py-0.5 text-zinc-500 hover:bg-zinc-900"
            }
            style={overlays[id] ? { borderColor: OVERLAY_TONE[id], color: OVERLAY_TONE[id] } : undefined}
          >
            {id}
          </button>
        ))}
        <button
          type="button"
          aria-pressed={showSpy}
          onClick={onToggleSpy}
          className={
            showSpy
              ? "rounded border border-sky-400 bg-sky-500/10 px-1.5 py-0.5 text-sky-300"
              : "rounded border border-zinc-700 px-1.5 py-0.5 text-zinc-500 hover:bg-zinc-900"
          }
        >
          SPY
        </button>
        <button
          type="button"
          aria-pressed={logScale}
          onClick={onToggleLog}
          className={
            logScale
              ? "rounded border border-zinc-500 bg-zinc-800 px-1.5 py-0.5 text-zinc-100"
              : "rounded border border-zinc-700 px-1.5 py-0.5 text-zinc-500 hover:bg-zinc-900"
          }
        >
          {logScale ? "Log" : "Linear"}
        </button>
      </div>
    </div>
  );
}

type IndicatorRowWithTime = IndicatorRow & { _t: UTCTimestamp };

function ChartCanvas({
  candles,
  volume,
  indicators,
  overlays,
  spyLine,
  logScale,
  onCrosshairMove,
}: {
  candles: CandlestickData<UTCTimestamp>[];
  volume: HistogramData<UTCTimestamp>[];
  indicators: IndicatorRowWithTime[];
  overlays: Record<OverlayId, boolean>;
  spyLine: LineData<UTCTimestamp>[];
  logScale: boolean;
  onCrosshairMove: (time: UTCTimestamp | null) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick", Time> | null>(null);
  const volSeriesRef = useRef<ISeriesApi<"Histogram", Time> | null>(null);
  const overlaySeriesRef = useRef<Map<string, ISeriesApi<"Line", Time>>>(new Map());

  // Mount chart once.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      layout: {
        background: { color: "#0a0a0a" },
        textColor: "#a1a1aa",
        fontFamily:
          'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
      },
      grid: {
        vertLines: { color: "rgba(63, 63, 70, 0.3)" },
        horzLines: { color: "rgba(63, 63, 70, 0.3)" },
      },
      rightPriceScale: { borderColor: "#3f3f46" },
      timeScale: { borderColor: "#3f3f46" },
      autoSize: true,
      crosshair: { mode: 1 },
    });
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderVisible: false,
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
    });
    const vol = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });
    chartRef.current = chart;
    candleSeriesRef.current = candle;
    volSeriesRef.current = vol;
    // Initial main-pane stretch — overridden by the overlay effect once
    // sub-panes start appearing. See the adaptive stretch logic that keeps
    // each sub-pane ~90px regardless of count.
    chart.panes()[0].setStretchFactor(5);
    const overlayMap = overlaySeriesRef.current;

    // Forward crosshair time to the parent so the OHLCV readout above the
    // chart can lookup the bar at cursor. `param.time` is `undefined` while
    // outside the data range / on mouse leave — surface that as null. The
    // chart is generic over `Time`; we pass UTCTimestamps in setData so the
    // runtime value is always a number.
    const handler = (param: MouseEventParams<Time>) => {
      const t = param.time;
      onCrosshairMove(typeof t === "number" ? (t as UTCTimestamp) : null);
    };
    chart.subscribeCrosshairMove(handler);

    return () => {
      chart.unsubscribeCrosshairMove(handler);
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volSeriesRef.current = null;
      overlayMap.clear();
    };
  }, [onCrosshairMove]);

  // Toggle log/linear price scale.
  useEffect(() => {
    chartRef.current?.priceScale("right").applyOptions({
      mode: logScale ? 1 : 0,
    });
  }, [logScale]);

  // Push data on every change. Lightweight Charts sorts internally, but we
  // emit data already-sorted from the API to be safe.
  useEffect(() => {
    candleSeriesRef.current?.setData(candles);
    volSeriesRef.current?.setData(volume);
    chartRef.current?.timeScale().fitContent();
  }, [candles, volume]);

  // Indicator overlays. Same-pane series (SMA / BB / SPY) update incrementally;
  // sub-pane series (RSI / ATR / OBV) are torn down and rebuilt every time
  // because lightweight-charts doesn't allow gaps in pane indices — the
  // `paneIndex` argument is effectively "place in this pane *or the next
  // available one*", so toggling indicators in arbitrary order (e.g. OBV
  // first → ATR → RSI) collapses two of them onto the same pane. The fix is
  // to always assign sub-panes contiguous indices 1..N in canonical order.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;

    const colorByKey: Record<string, string> = {
      SMA20: "#fbbf24",
      SMA50: "#f59e0b",
      SMA200: "#d97706",
      BB_UPPER: OVERLAY_TONE.BB,
      BB_MIDDLE: OVERLAY_TONE.BB,
      BB_LOWER: OVERLAY_TONE.BB,
      RSI: OVERLAY_TONE.RSI,
      ATR: OVERLAY_TONE.ATR,
      OBV: OVERLAY_TONE.OBV,
      SPY: "#38bdf8",
    };

    // ── Same-pane overlays (incremental) ─────────────────────────────────
    const sameDesired: Record<string, LineData<UTCTimestamp>[]> = {};
    if (overlays.SMA) {
      sameDesired["SMA20"] = indicatorLine(indicators, "sma_20");
      sameDesired["SMA50"] = indicatorLine(indicators, "sma_50");
      sameDesired["SMA200"] = indicatorLine(indicators, "sma_200");
    }
    if (overlays.BB) {
      sameDesired["BB_UPPER"] = indicatorLine(indicators, "bb_upper");
      sameDesired["BB_MIDDLE"] = indicatorLine(indicators, "bb_middle");
      sameDesired["BB_LOWER"] = indicatorLine(indicators, "bb_lower");
    }
    sameDesired["SPY"] = spyLine;

    // Every series key that lives in a sub-pane. MACD has TWO lines (line +
    // signal) sharing the same pane; both keys must be tracked so the
    // teardown pass clears them before rebuild.
    const SUB_PANE_KEYS = new Set(["RSI", "ATR", "OBV", "MACD", "MACD_SIGNAL"]);

    // Remove same-pane series that are no longer desired (skip sub-pane
    // entries — they're handled by the rebuild step below).
    for (const [key, series] of overlaySeriesRef.current) {
      if (SUB_PANE_KEYS.has(key)) continue;
      if (!sameDesired[key] || sameDesired[key].length === 0) {
        chart.removeSeries(series);
        overlaySeriesRef.current.delete(key);
      }
    }
    // Add or update same-pane series.
    for (const [key, data] of Object.entries(sameDesired)) {
      if (data.length === 0) continue;
      let series = overlaySeriesRef.current.get(key);
      if (!series) {
        const isSpy = key === "SPY";
        const isBbMiddle = key === "BB_MIDDLE";
        const seriesOptions = {
          color: colorByKey[key] ?? "#a1a1aa",
          lineWidth: 1 as const,
          // Dashed middle band visually disambiguates it from the solid
          // upper / lower envelope — same hue, distinct stroke.
          lineStyle: isBbMiddle ? LineStyle.Dashed : LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: false,
        };
        if (isSpy) {
          series = chart.addSeries(LineSeries, {
            ...seriesOptions,
            priceScaleId: "spy_compare",
          });
          chart.priceScale("spy_compare").applyOptions({
            scaleMargins: { top: 0.05, bottom: 0.25 },
            visible: false,
          });
        } else {
          series = chart.addSeries(LineSeries, {
            ...seriesOptions,
            priceScaleId: "right",
          });
        }
        overlaySeriesRef.current.set(key, series);
      }
      series.setData(data);
    }

    // ── Sub-pane indicators (rebuild every change) ───────────────────────
    // Tear down all tracked sub-pane series first so empty panes can be
    // collapsed and the next add-pass starts from index 1.
    for (const key of SUB_PANE_KEYS) {
      const existing = overlaySeriesRef.current.get(key);
      if (existing) {
        chart.removeSeries(existing);
        overlaySeriesRef.current.delete(key);
      }
    }
    // Drop any panes left behind by the teardown so addSeries(...,N) starts
    // numbering from 1 again. Iterate from the highest index down so the
    // remaining panes don't get re-indexed underneath us.
    const panesAfterTeardown = chart.panes();
    for (let i = panesAfterTeardown.length - 1; i >= 1; i--) {
      chart.removePane(i);
    }

    // A sub-pane can host one OR more line series (MACD has line + signal).
    // Each entry's `lines` array maps to one pane; pane index is the entry's
    // index in `activeSubs` plus 1.
    type SubLine = { key: string; field: keyof IndicatorRow; color: string };
    const activeSubs: { lines: SubLine[] }[] = [];
    if (overlays.RSI)
      activeSubs.push({
        lines: [{ key: "RSI", field: "rsi_14", color: OVERLAY_TONE.RSI }],
      });
    if (overlays.MACD)
      activeSubs.push({
        lines: [
          { key: "MACD", field: "macd", color: OVERLAY_TONE.MACD },
          // Signal line (slow) in red for the standard fast/slow contrast —
          // blue MACD line crossing red signal line is the visual cue.
          { key: "MACD_SIGNAL", field: "macd_signal", color: "#f87171" },
        ],
      });
    if (overlays.ATR)
      activeSubs.push({
        lines: [{ key: "ATR", field: "atr_14", color: OVERLAY_TONE.ATR }],
      });
    if (overlays.OBV)
      activeSubs.push({
        lines: [{ key: "OBV", field: "obv", color: OVERLAY_TONE.OBV }],
      });
    activeSubs.forEach((sub, i) => {
      const paneIndex = i + 1;
      sub.lines.forEach((line) => {
        const data = indicatorLine(indicators, line.field);
        if (data.length === 0) return;
        const series = chart.addSeries(
          LineSeries,
          {
            color: line.color,
            lineWidth: 1 as const,
            priceLineVisible: false,
            lastValueVisible: true,
          },
          paneIndex,
        );
        series.setData(data);
        overlaySeriesRef.current.set(line.key, series);
      });
    });

    // Adaptive main-pane stretch — keeps each sub-pane usable regardless of
    // how many of {RSI, MACD, ATR, OBV} are toggled on. With chart height
    // 400 the math at stretch n:1 across (1 + subs) panes is:
    //   0 subs: main full
    //   1 sub:  main 4 / sub 1     → main 320, sub 80
    //   2 subs: main 3 / sub 1 ea  → main 240, sub 80 ea
    //   3 subs: main 2 / sub 1 ea  → main 160, sub 80 ea
    //   4 subs: main 2 / sub 1 ea  → main 133, sub 67 ea (clamped)
    const subPaneCount = activeSubs.length;
    const mainStretch = subPaneCount === 0 ? 5 : Math.max(2, 5 - subPaneCount);
    chart.panes()[0].setStretchFactor(mainStretch);
  }, [indicators, overlays, spyLine]);

  // Fixed height — toggling RSI / ATR / OBV no longer pushes the cards row
  // below. The adaptive stretch factor (see indicator effect) keeps each
  // sub-pane ~80px regardless of how many are toggled.
  const heightPx = 400;

  return (
    <div
      ref={containerRef}
      className="w-full"
      style={{ height: `${heightPx}px` }}
      data-testid="price-chart-canvas"
    />
  );
}

// ─── OHLCV crosshair readout ───────────────────────────────────────────────
//
// Renders a small monospace bar of O / H / L / C / Vol values either for the
// candle currently under the cursor or — when the cursor is off-chart — the
// most recent visible bar. Mirrors the upper-left readout bar in the full
// TradingView app, the way users read a candlestick chart at a glance.

function OhlcReadout({
  candles,
  volume,
  indicators,
  overlays,
  hovered,
}: {
  candles: CandlestickData<UTCTimestamp>[];
  volume: HistogramData<UTCTimestamp>[];
  indicators: IndicatorRowWithTime[];
  overlays: Record<OverlayId, boolean>;
  hovered: UTCTimestamp | null;
}) {
  const targetTime = hovered ?? candles[candles.length - 1]?.time;
  if (targetTime === undefined) {
    return <div className="h-[34px]" aria-hidden />;
  }

  const candle = candles.find((c) => c.time === targetTime);
  const vol = volume.find((v) => v.time === targetTime);
  if (!candle) {
    return <div className="h-[34px]" aria-hidden />;
  }

  const upDay = candle.close >= candle.open;
  const tone = upDay ? "text-emerald-400" : "text-red-400";
  const change = candle.close - candle.open;
  const changePct = candle.open === 0 ? null : (change / candle.open) * 100;
  const dateLabel = new Date((targetTime as number) * 1000).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });

  // Indicator values at the hovered bar — used by the second readout row to
  // surface SMA / BB numbers the same way OHLC does for the candle. Chooses
  // the indicator row whose timestamp matches; for weekly / monthly the
  // candle and indicator timestamps line up (both come from the same
  // aggregation tables) so an exact match works.
  const indRow = indicators.find((r) => r._t === targetTime);
  const indParts: { label: string; value: string; color: string }[] = [];
  if (overlays.SMA && indRow) {
    if (indRow.sma_20 !== null)
      indParts.push({ label: "SMA20", value: indRow.sma_20.toFixed(2), color: "#fbbf24" });
    if (indRow.sma_50 !== null)
      indParts.push({ label: "SMA50", value: indRow.sma_50.toFixed(2), color: "#f59e0b" });
    if (indRow.sma_200 !== null)
      indParts.push({ label: "SMA200", value: indRow.sma_200.toFixed(2), color: "#d97706" });
  }
  if (overlays.BB && indRow) {
    if (indRow.bb_upper !== null)
      indParts.push({ label: "BB↑", value: indRow.bb_upper.toFixed(2), color: OVERLAY_TONE.BB });
    if (indRow.bb_middle !== null)
      indParts.push({ label: "BB·", value: indRow.bb_middle.toFixed(2), color: OVERLAY_TONE.BB });
    if (indRow.bb_lower !== null)
      indParts.push({ label: "BB↓", value: indRow.bb_lower.toFixed(2), color: OVERLAY_TONE.BB });
  }
  if (overlays.MACD && indRow) {
    if (indRow.macd !== null)
      indParts.push({ label: "MACD", value: indRow.macd.toFixed(2), color: OVERLAY_TONE.MACD });
    if (indRow.macd_signal !== null)
      indParts.push({ label: "Sig", value: indRow.macd_signal.toFixed(2), color: "#f87171" });
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex flex-col gap-px pb-1 font-mono text-[11px] tabular-nums text-zinc-300"
    >
      <div className="flex h-[16px] flex-wrap items-baseline gap-x-3">
        <span className="text-zinc-500">{dateLabel}</span>
        <span>
          <span className="text-zinc-500">O</span> {candle.open.toFixed(2)}
        </span>
        <span>
          <span className="text-zinc-500">H</span> {candle.high.toFixed(2)}
        </span>
        <span>
          <span className="text-zinc-500">L</span> {candle.low.toFixed(2)}
        </span>
        <span className={tone}>
          <span className="text-zinc-500">C</span> {candle.close.toFixed(2)}
        </span>
        <span className={tone}>
          {change >= 0 ? "+" : ""}
          {change.toFixed(2)}
          {changePct !== null ? ` (${change >= 0 ? "+" : ""}${changePct.toFixed(2)}%)` : ""}
        </span>
        {vol ? (
          <span>
            <span className="text-zinc-500">Vol</span>{" "}
            {(vol.value / 1_000_000).toFixed(2)}M
          </span>
        ) : null}
      </div>
      <div className="flex h-[16px] flex-wrap items-baseline gap-x-3 text-[10px]">
        {indParts.length === 0 ? (
          <span className="text-zinc-700">&nbsp;</span>
        ) : (
          indParts.map((p) => (
            <span key={p.label}>
              <span className="text-zinc-500">{p.label}</span>{" "}
              <span style={{ color: p.color }}>{p.value}</span>
            </span>
          ))
        )}
      </div>
    </div>
  );
}
