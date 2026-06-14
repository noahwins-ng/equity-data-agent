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
 * News card date label: the absolute date only, e.g. `Apr 28`.
 *
 * QNT-252: this used to prepend a relative bucket (`Today` / `Yesterday` /
 * `2d ago`) computed against `new Date()`. NewsCard is a server component
 * baked into the statically built ticker page (`dynamic = "force-static"`,
 * refreshed only by the Vercel deploy hook), so that `new Date()` was frozen
 * at BUILD time — an article built as "Today" still read "Today" days later.
 *
 * View-time correctness is the requirement. We drop the relative bucket in the
 * SSG path rather than hydrate a client-side clock: the news list is otherwise
 * a pure server component, and within the 7-day window an absolute `Apr 28`
 * carries the same information without dragging a hydration boundary (and its
 * flash + JS) into the card for a cosmetic prefix.
 */
export function formatNewsDate(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

export function changeColorClass(v: number | null | undefined): string {
  if (isMissing(v) || v === 0) return "text-zinc-400";
  return (v as number) > 0 ? "text-emerald-400" : "text-red-400";
}
