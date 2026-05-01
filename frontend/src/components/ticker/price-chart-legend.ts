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

function fmtBigNum(v: number | null | undefined, dp: number): string {
  // Used only by obvLegendText — OBV is the one indicator whose magnitude
  // benefits from K/M/B compression. Volume is shown in the OHLCV header
  // above the chart, not as a per-pane legend.
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

export function mainLegendText(
  ind: IndicatorRow | undefined,
  showSma: boolean,
  showBb: boolean,
  spyChangePct: number | null,
): string {
  // One row per toggled-on indicator group (SMA / BB / SPY). The ticker is
  // already shown in the quote header above the chart, so it's omitted here.
  // Returns an empty string when no overlays are active — caller renders that
  // as an empty div with no visible artifact.
  const rows: string[] = [];
  if (showSma && ind) {
    const smas: string[] = [];
    if (ind.sma_20 !== null) smas.push(`SMA20 ${ind.sma_20.toFixed(2)}`);
    if (ind.sma_50 !== null) smas.push(`SMA50 ${ind.sma_50.toFixed(2)}`);
    if (ind.sma_200 !== null) smas.push(`SMA200 ${ind.sma_200.toFixed(2)}`);
    if (smas.length > 0) rows.push(smas.join("   "));
  }
  if (showBb && ind) {
    const bbs: string[] = [];
    if (ind.bb_upper !== null) bbs.push(`BB↑ ${ind.bb_upper.toFixed(2)}`);
    if (ind.bb_middle !== null) bbs.push(`BB· ${ind.bb_middle.toFixed(2)}`);
    if (ind.bb_lower !== null) bbs.push(`BB↓ ${ind.bb_lower.toFixed(2)}`);
    if (bbs.length > 0) rows.push(bbs.join("   "));
  }
  if (spyChangePct !== null && !Number.isNaN(spyChangePct)) {
    const sign = spyChangePct >= 0 ? "+" : "";
    rows.push(`SPY ${sign}${spyChangePct.toFixed(2)}%`);
  }
  return rows.join("\n");
}
