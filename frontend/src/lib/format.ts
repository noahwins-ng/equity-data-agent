/**
 * Number / percent / currency / date formatters for the ticker detail page.
 *
 * Single source of truth so the quote header, technicals card, fundamentals
 * card, and news card render values identically — important on a dense
 * monospaced surface where mismatched precision is visually loud.
 *
 * Conventions:
 *   - `null` / `undefined` / `NaN` always render as `"—"` (em-dash).
 *   - Percentages are passed in as percent values (e.g. `5.4` for 5.4%, NOT 0.054).
 *   - Currency / volume use compact suffixes (K/M/B/T) above ~10k.
 *   - Bps deltas are signed integers with a `bps` suffix.
 */

const DASH = "—";

export function isMissing(v: number | null | undefined): boolean {
  return v === null || v === undefined || Number.isNaN(v);
}

export function formatPrice(v: number | null | undefined, precision = 2): string {
  if (isMissing(v)) return DASH;
  return (v as number).toLocaleString("en-US", {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  });
}

export function formatSignedPct(v: number | null | undefined, precision = 2): string {
  if (isMissing(v)) return DASH;
  const value = v as number;
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(precision)}%`;
}

export function formatPct(v: number | null | undefined, precision = 1): string {
  if (isMissing(v)) return DASH;
  return `${(v as number).toFixed(precision)}%`;
}

export function formatBps(v: number | null | undefined): string {
  if (isMissing(v)) return DASH;
  const value = Math.round(v as number);
  const sign = value > 0 ? "+" : "";
  return `${sign}${value} bps`;
}

/**
 * Compact human-readable representation of large numbers (volume, market cap,
 * revenue). Falls back to plain locale formatting below ~10k.
 */
export function formatCompact(v: number | null | undefined): string {
  if (isMissing(v)) return DASH;
  const value = v as number;
  const abs = Math.abs(value);
  if (abs < 1_000) return value.toFixed(0);
  if (abs < 10_000)
    return value.toLocaleString("en-US", {
      maximumFractionDigits: 0,
    });
  if (abs < 1_000_000) return `${(value / 1_000).toFixed(1)}K`;
  if (abs < 1_000_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (abs < 1_000_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}B`;
  return `${(value / 1_000_000_000_000).toFixed(2)}T`;
}

export function formatRatio(v: number | null | undefined, precision = 2): string {
  if (isMissing(v)) return DASH;
  return (v as number).toFixed(precision);
}

/**
 * Render an ISO date (`YYYY-MM-DD`) as `Mon DD, YYYY` for the quote-header
 * `close <date>` framing. Locale fixed to `en-US` so server + client agree
 * regardless of where Vercel renders.
 */
export function formatAsOfDate(iso: string | null | undefined): string {
  if (!iso) return DASH;
  // Parse as a UTC midnight to avoid the off-by-one when the server's
  // timezone is west of UTC and the date string is interpreted as a Date in
  // local time. (e.g. "2026-04-28" in PST -> Apr 27 14:00 local.)
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
}

/**
 * News card date label: `Today · Apr 30`, `Yesterday · Apr 29`, `2d ago · Apr 28`,
 * or just `Apr 22` once we're past the 7-day relative window.
 */
export function formatNewsDate(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  const absolute = d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
  // Compare in UTC so the relative bucket lines up with the absolute label
  // (which is also UTC-formatted). Mixing local and UTC here was the
  // "Yesterday · Apr 29" / "2d ago · Apr 29" inconsistency.
  const todayUtc = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const thatUtc = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  const dayMs = 24 * 60 * 60 * 1000;
  const diffDays = Math.round((todayUtc - thatUtc) / dayMs);
  if (diffDays === 0) return `Today · ${absolute}`;
  if (diffDays === 1) return `Yesterday · ${absolute}`;
  if (diffDays < 7) return `${diffDays}d ago · ${absolute}`;
  return absolute;
}

export function changeColorClass(v: number | null | undefined): string {
  if (isMissing(v) || v === 0) return "text-zinc-400";
  return (v as number) > 0 ? "text-emerald-400" : "text-red-400";
}
