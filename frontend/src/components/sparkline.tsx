/**
 * Inline-SVG sparkline for the watchlist rows.
 *
 * Per ADR-014 §1: no chart library — 60 datapoints, no axes, no
 * interactivity. The component is a pure server-renderable function so the
 * watchlist stays a Server Component (no `"use client"` boundary).
 *
 * `values` are oldest-first (left to right), matching the
 * `/dashboard/summary` `sparkline` array contract. An empty / single-point
 * series renders an empty <svg> placeholder rather than crashing — the
 * watchlist row otherwise stays renderable so a missing-OHLCV ticker still
 * shows symbol + name.
 */

const STROKE_WIDTH = 1.25;

export type SparklineProps = {
  values: number[];
  width?: number;
  height?: number;
  /**
   * Stroke colour, derived from the daily change sign by the parent so a
   * single source of truth (the row's positive/negative tint) drives both
   * the price-change pill and the sparkline.
   */
  stroke: string;
  className?: string;
  ariaLabel?: string;
};

export function Sparkline({
  values,
  width = 80,
  height = 28,
  stroke,
  className,
  ariaLabel,
}: SparklineProps) {
  if (values.length < 2) {
    return (
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className={className}
        aria-label={ariaLabel}
        role="img"
      />
    );
  }

  // Symmetric vertical pad so the stroke isn't clipped at the extremes.
  const pad = STROKE_WIDTH;
  const innerHeight = height - pad * 2;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const stepX = width / (values.length - 1);

  const points = values
    .map((v, i) => {
      const x = i * stepX;
      const y = pad + (1 - (v - min) / range) * innerHeight;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={className}
      aria-label={ariaLabel}
      role="img"
    >
      <polyline
        fill="none"
        stroke={stroke}
        strokeWidth={STROKE_WIDTH}
        strokeLinecap="round"
        strokeLinejoin="round"
        points={points}
      />
    </svg>
  );
}
