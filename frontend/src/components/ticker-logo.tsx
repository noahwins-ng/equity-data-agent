"use client";

/**
 * Logo cell for a watchlist row — renders the company logo from the
 * `/api/v1/logos` mapping, falling back to ticker initials in a colored
 * circle when the URL is null OR the image fails to load at runtime
 * (broken-image icon is forbidden by the watchlist polish AC).
 *
 * Client component only because `<img onError>` needs JS to swap to the
 * fallback. The parent watchlist stays a server component; the URL is
 * passed in as a prop so the bundle ships no fetch logic.
 */

import { useState } from "react";

type Props = {
  ticker: string;
  logoUrl: string | null;
  size?: number;
};

// Stable palette for the initials circle. Hash on the ticker string so the
// same symbol gets the same color across reloads — eight muted hues that
// sit well next to the zinc UI without competing with the change-pct text.
const PALETTE = [
  "bg-sky-700",
  "bg-emerald-700",
  "bg-amber-700",
  "bg-rose-700",
  "bg-violet-700",
  "bg-teal-700",
  "bg-fuchsia-700",
  "bg-orange-700",
];

function paletteColor(ticker: string): string {
  let hash = 0;
  for (const ch of ticker) {
    hash = (hash * 31 + ch.charCodeAt(0)) | 0;
  }
  return PALETTE[Math.abs(hash) % PALETTE.length]!;
}

function initials(ticker: string): string {
  return ticker.slice(0, 2);
}

export function TickerLogo({ ticker, logoUrl, size = 24 }: Props) {
  const [errored, setErrored] = useState(false);
  const showFallback = !logoUrl || errored;

  if (showFallback) {
    return (
      <span
        aria-hidden="true"
        className={`inline-flex shrink-0 items-center justify-center rounded-full font-mono text-[10px] font-semibold uppercase text-zinc-50 ${paletteColor(ticker)}`}
        style={{ width: size, height: size }}
      >
        {initials(ticker)}
      </span>
    );
  }

  return (
    // `logoUrl` is a `data:` URL inlined by /api/v1/logos — `<img>` over
    // `next/image` because the optimisation pipeline can't help an inline
    // base64 string and the per-render decoration is tiny anyway. The
    // onError swap to initials is the AC; `loading="eager"` because every
    // render site (watchlist rows, quote header) is above the fold.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={logoUrl}
      alt=""
      width={size}
      height={size}
      loading="eager"
      onError={() => setErrored(true)}
      className="shrink-0 rounded-full border border-zinc-700 object-contain"
      style={{ width: size, height: size }}
    />
  );
}
