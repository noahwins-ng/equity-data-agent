// Run with: `npm test` (from `frontend/`) — uses Node 24's built-in TS loader
// and node:test runner so no vitest/jest dependency is needed.

import test from "node:test";
import assert from "node:assert/strict";

import type { IndicatorRow } from "@/lib/api";

import {
  atrLegendText,
  macdLegendText,
  mainLegendHtml,
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

test("mainLegendHtml shows toggled overlays only", () => {
  // No overlays: empty (ticker is in the quote header above).
  assert.equal(mainLegendHtml(row({}), false, false, false), "");

  // SMA only: each SMA wrapped in its line's own colour span.
  assert.equal(
    mainLegendHtml(
      row({ sma_20: 199.5, sma_50: 197.1, sma_200: 184.0 }),
      true,
      false,
      false,
    ),
    [
      `<span style="color:#fbbf24">SMA20 199.50</span>`,
      `<span style="color:#f59e0b">SMA50 197.10</span>`,
      `<span style="color:#d97706">SMA200 184.00</span>`,
    ].join("   "),
  );

  // BB only: all three bands share the same violet hue.
  assert.equal(
    mainLegendHtml(
      row({ bb_upper: 220.4, bb_middle: 198.2, bb_lower: 176.0 }),
      false,
      true,
      false,
    ),
    [
      `<span style="color:#a78bfa">BB↑ 220.40</span>`,
      `<span style="color:#a78bfa">BB· 198.20</span>`,
      `<span style="color:#a78bfa">BB↓ 176.00</span>`,
    ].join("   "),
  );

  // SPY only: sky-blue label, no value.
  assert.equal(
    mainLegendHtml(row({}), false, false, true),
    `<span style="color:#38bdf8">SPY</span>`,
  );

  // SMA + BB + SPY all on: three rows separated by <br>.
  assert.equal(
    mainLegendHtml(
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
      true,
    ),
    [
      [
        `<span style="color:#fbbf24">SMA20 199.50</span>`,
        `<span style="color:#f59e0b">SMA50 197.10</span>`,
        `<span style="color:#d97706">SMA200 184.00</span>`,
      ].join("   "),
      [
        `<span style="color:#a78bfa">BB↑ 220.40</span>`,
        `<span style="color:#a78bfa">BB· 198.20</span>`,
        `<span style="color:#a78bfa">BB↓ 176.00</span>`,
      ].join("   "),
      `<span style="color:#38bdf8">SPY</span>`,
    ].join("<br>"),
  );
});
