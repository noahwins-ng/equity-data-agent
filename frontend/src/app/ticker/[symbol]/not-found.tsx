import Link from "next/link";

export default function NotFound() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
      <h1 className="text-xl font-semibold text-zinc-100">Ticker not found</h1>
      <p className="text-sm text-zinc-400">
        The symbol you visited isn&apos;t in this watchlist.
      </p>
      <Link
        href="/"
        className="text-xs uppercase tracking-wider text-emerald-400 hover:underline"
      >
        Back to landing
      </Link>
    </div>
  );
}
