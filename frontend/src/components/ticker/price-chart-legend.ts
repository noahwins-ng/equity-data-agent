// Pure legend-text formatters for the price chart.
//
// Lives in its own module so it can be exercised by a Node test runner without
// pulling in lightweight-charts (DOM-bound canvas) or React. Type-only imports
// from `@/lib/api` are stripped at runtime by Node 24's TS loader, so the
// alias never needs to resolve outside the bundler.

import type { IndicatorRow } from "@/lib/api";

function fmtNum(v: number | null | undefined, dp: number): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  return v.toFixed(dp);
}

// Exported so the chart can reuse the same K/M/B compaction in OBV's right-
// axis price formatter — keeps the axis label short for values like 5.92B.
export function fmtBigNum(v: number | null | undefined, dp: number): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  const abs = Math.abs(v);
  if (abs >= 1e9) return (v / 1e9).toFixed(dp) + "B";
  if (abs >= 1e6) return (v / 1e6).toFixed(dp) + "M";
  if (abs >= 1e3) return (v / 1e3).toFixed(dp) + "K";
  return v.toFixed(0);
}

export function rsiLegendText(ind: IndicatorRow | undefined): string {
  return `RSI(14)  ${fmtNum(ind?.rsi_14, 1)}`;
}

export function macdLegendText(ind: IndicatorRow | undefined): string {
  // macd_hist is precomputed in the indicator pipeline (macd - macd_signal).
  return `MACD  ${fmtNum(ind?.macd, 2)}  /  Signal  ${fmtNum(ind?.macd_signal, 2)}  /  Hist  ${fmtNum(ind?.macd_hist, 2)}`;
}

export function atrLegendText(ind: IndicatorRow | undefined): string {
  return `ATR(14)  ${fmtNum(ind?.atr_14, 2)}`;
}

export function obvLegendText(ind: IndicatorRow | undefined): string {
  return `OBV  ${fmtBigNum(ind?.obv, 1)}`;
}

// Legend label colours — each must match the corresponding chart series so
// the user can visually link a number in the legend to the line on the
// chart. Keep these in sync with the colorByKey map in price-chart.tsx.
const SMA20_COLOR = "#fbbf24"; // amber-400
const SMA50_COLOR = "#f59e0b"; // amber-500
const SMA200_COLOR = "#d97706"; // amber-600
const BB_COLOR = "#a78bfa"; // violet-400 (same hue for upper / middle / lower)
const SPY_LABEL_COLOR = "#38bdf8"; // sky-400 — matches the SPY chip + line

function colorSpan(text: string, color: string): string {
  return `<span style="color:${color}">${text}</span>`;
}

export function mainLegendHtml(
  ind: IndicatorRow | undefined,
  showSma: boolean,
  showBb: boolean,
  showSpy: boolean,
): string {
  // One row per toggled-on indicator group (SMA / BB / SPY). The ticker is
  // already shown in the quote header above the chart, so it's omitted here.
  // SPY shows a label only — no %; the previous % anchored to the first
  // loaded bar and shifted with the range preset, so the value was
  // misleading. The SPY series + its right-axis price label still carry the
  // comparison signal. Returns HTML so the SPY row can be coloured to match
  // the SPY chip / line; values come from controlled sources (no user
  // input), so XSS surface is nil.
  const rows: string[] = [];
  if (showSma && ind) {
    const smas: string[] = [];
    if (ind.sma_20 !== null)
      smas.push(colorSpan(`SMA20 ${ind.sma_20.toFixed(2)}`, SMA20_COLOR));
    if (ind.sma_50 !== null)
      smas.push(colorSpan(`SMA50 ${ind.sma_50.toFixed(2)}`, SMA50_COLOR));
    if (ind.sma_200 !== null)
      smas.push(colorSpan(`SMA200 ${ind.sma_200.toFixed(2)}`, SMA200_COLOR));
    if (smas.length > 0) rows.push(smas.join("   "));
  }
  if (showBb && ind) {
    const bbs: string[] = [];
    if (ind.bb_upper !== null)
      bbs.push(colorSpan(`BB↑ ${ind.bb_upper.toFixed(2)}`, BB_COLOR));
    if (ind.bb_middle !== null)
      bbs.push(colorSpan(`BB· ${ind.bb_middle.toFixed(2)}`, BB_COLOR));
    if (ind.bb_lower !== null)
      bbs.push(colorSpan(`BB↓ ${ind.bb_lower.toFixed(2)}`, BB_COLOR));
    if (bbs.length > 0) rows.push(bbs.join("   "));
  }
  if (showSpy) rows.push(colorSpan("SPY", SPY_LABEL_COLOR));
  return rows.join("<br>");
}
