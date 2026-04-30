/**
 * News card — last-7d Finnhub headlines for the ticker.
 *
 * Server component; reads `/api/v1/news/{ticker}?days=7` once per page render.
 * No sentiment chip in v1 — see ADR-015 §"Revision history" (2026-04-28).
 *
 * Per ADR-014 anti-pattern §5: empty array and "service down" render
 * identically. The frontend doesn't differentiate.
 */

import type { NewsRow } from "@/lib/api";
import { formatNewsDate } from "@/lib/format";

const NEWS_WINDOW_DAYS = 7;
const NEWS_SOURCE_LABEL = "Finnhub";

export function NewsCard({ items }: { items: NewsRow[] }) {
  return (
    <section
      aria-label="News"
      className="flex min-h-0 flex-col rounded border border-zinc-800 bg-zinc-950"
    >
      <header className="flex shrink-0 items-baseline justify-between border-b border-zinc-800 px-4 py-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-200">
          News
        </h2>
        <span className="text-[10px] uppercase tracking-wider text-zinc-500">
          Last {NEWS_WINDOW_DAYS}d · {items.length} · {NEWS_SOURCE_LABEL}
        </span>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        {items.length === 0 ? (
          <p className="px-1 text-sm text-zinc-500">No recent news.</p>
        ) : (
          <ul className="space-y-3">
            {items.map((item) => (
              <li key={item.id}>
                <NewsItem item={item} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

/**
 * Pill label for a news item.
 *
 * Two distinct cases live in the warehouse:
 *
 *   1. Finnhub-tagged articles where `publisher_name` is "Yahoo" /
 *      "Benzinga" / "CNBC" / etc. and the URL is `finnhub.io/...`. Finnhub
 *      redirects to wherever — we can't see the original outlet, so the
 *      Finnhub label is the best signal we have, even though "Yahoo"
 *      could syndicate from Reuters / Bloomberg / Fool / etc.
 *   2. Articles with empty `publisher_name` and a *direct* outlet URL
 *      (finance.yahoo.com, fool.com, marketwatch.com, wsj.com, …). Here
 *      the URL host is more accurate than what Finnhub's feed-source field
 *      reports. The legacy fallback to `item.source` ("finnhub") was
 *      mislabeling ~28% of weekly articles as "FINNHUB" — the host strips
 *      `www.` and surfaces the actual serving domain instead.
 */
function publisherLabel(item: NewsRow): string {
  const host = item.host?.replace(/^www\./, "") ?? "";
  if (host && host !== "finnhub.io") return host;
  return item.publisher_name?.trim() || "—";
}

function NewsItem({ item }: { item: NewsRow }) {
  const publisher = publisherLabel(item);

  return (
    <a
      href={item.url}
      target="_blank"
      rel="noopener noreferrer"
      className="group flex rounded p-1 transition hover:bg-zinc-900 focus-visible:bg-zinc-900 focus-visible:outline-none"
    >
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-baseline gap-2 text-[10px] uppercase tracking-wider">
          <span className="rounded border border-zinc-700 px-1.5 py-0.5 text-zinc-300">
            {publisher}
          </span>
          <span className="text-zinc-500">{formatNewsDate(item.published_at)}</span>
        </div>
        <h3 className="text-sm font-medium text-zinc-100 group-hover:text-emerald-300">
          {item.headline}
        </h3>
        {item.body ? (
          <p className="line-clamp-2 text-xs text-zinc-400">{item.body}</p>
        ) : null}
      </div>
    </a>
  );
}
