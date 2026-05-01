// Run with: `npm test` (from `frontend/`) — uses Node 24's built-in TS loader
// and node:test runner so no vitest/jest dependency is needed.

import test from "node:test";
import assert from "node:assert/strict";

import type { IndicatorRow } from "@/lib/api";

import {
  atrLegendText,
  macdLegendText,
  mainLegendText,
  obvLegendText,
  rsiLegendText,
} from "./price-chart-legend.ts";

function row(overrides: Partial<IndicatorRow>): IndicatorRow {
  return {
    time: "2026-04-30",
    sma_20: null,
    sma_50: null,
    sma_200: null,
    ema_12: null,
    ema_26: null,
    rsi_14: null,
    macd: null,
    macd_signal: null,
    macd_hist: null,
    macd_bullish_cross: 0,
    bb_upper: null,
    bb_middle: null,
    bb_lower: null,
    bb_pct_b: null,
    adx_14: null,
    atr_14: null,
    obv: null,
    ...overrides,
  };
}

test("rsiLegendText shows the RSI value at the hovered bar", () => {
  // AC: "add one test that asserts the RSI legend renders the expected
  // value when the crosshair is at a known bar."
  assert.equal(rsiLegendText(row({ rsi_14: 62.4321 })), "RSI(14)  62.4");
});

test("rsiLegendText falls back to -- when the bar has no RSI yet", () => {
  // First 13 bars of any series have null RSI(14); legend must not crash.
  assert.equal(rsiLegendText(row({ rsi_14: null })), "RSI(14)  --");
  assert.equal(rsiLegendText(undefined), "RSI(14)  --");
});

test("macdLegendText renders line / signal / hist with two decimals", () => {
  assert.equal(
    macdLegendText(row({ macd: 1.2, macd_signal: 0.95, macd_hist: 0.25 })),
    "MACD  1.20  /  Signal  0.95  /  Hist  0.25",
  );
});

test("atrLegendText formats ATR with two decimals", () => {
  assert.equal(atrLegendText(row({ atr_14: 4.213 })), "ATR(14)  4.21");
});

test("obvLegendText scales to M/B with one decimal", () => {
  assert.equal(obvLegendText(row({ obv: 12_400_000 })), "OBV  12.4M");
  assert.equal(obvLegendText(row({ obv: 1_500_000_000 })), "OBV  1.5B");
});

test("mainLegendText shows toggled overlays only", () => {
  // No overlays, no SPY: empty (ticker is in the quote header above).
  assert.equal(mainLegendText(row({}), false, false, null), "");

  // SMA only: single SMA row.
  assert.equal(
    mainLegendText(
      row({ sma_20: 199.5, sma_50: 197.1, sma_200: 184.0 }),
      true,
      false,
      null,
    ),
    "SMA20 199.50   SMA50 197.10   SMA200 184.00",
  );

  // SMA + BB + SPY all on: 3 rows (SMAs / BB / SPY).
  assert.equal(
    mainLegendText(
      row({
        sma_20: 199.5,
        sma_50: 197.1,
        sma_200: 184.0,
        bb_upper: 220.4,
        bb_middle: 198.2,
        bb_lower: 176.0,
      }),
      true,
      true,
      2.3,
    ),
    "SMA20 199.50   SMA50 197.10   SMA200 184.00\nBB↑ 220.40   BB· 198.20   BB↓ 176.00\nSPY +2.30%",
  );

  // SPY only (no SMA, no BB): single SPY row.
  assert.equal(
    mainLegendText(row({}), false, false, 2.3),
    "SPY +2.30%",
  );

  // Negative SPY: bare minus, no double sign.
  assert.equal(
    mainLegendText(row({}), false, false, -1.4),
    "SPY -1.40%",
  );
});
