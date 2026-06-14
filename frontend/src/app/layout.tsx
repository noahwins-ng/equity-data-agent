import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Analytics } from "@vercel/analytics/next";
import { AppShell } from "@/components/app-shell";
import { Watchlist } from "@/components/watchlist";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Equity Data Agent",
  description: "Analyst workstation for the 10-ticker portfolio.",
};

// QNT-251: the app is fully dark. theme-color paints the mobile URL bar to
// match the zinc-950 body; colorScheme reinforces the globals.css rule so
// native controls render dark.
export const viewport: Viewport = {
  colorScheme: "dark",
  themeColor: "#09090b", // zinc-950, matches the body background
};

/**
 * Root layout — three-pane app shell (ADR-014).
 *
 *   +--------------------------------------------------------------+
 *   | watchlist (server) | route slot ({children}) | chat (client) |
 *   +--------------------------------------------------------------+
 *
 * The watchlist and chat panels are persistent across every route. The
 * middle slot swaps between `/` (landing) and `/ticker/[symbol]` without
 * tearing down the rails — critical for the chat SSE stream (Anti-pattern
 * #6).
 */
export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="relative flex h-full flex-col overflow-hidden bg-zinc-950 text-zinc-100">
        {/* QNT-251: first tab stop — visually hidden until focused, then jumps
            keyboard users past the watchlist rail straight to <main id="main">. */}
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-[60] focus:rounded focus:border focus:border-emerald-500/60 focus:bg-zinc-900 focus:px-3 focus:py-2 focus:font-mono focus:text-xs focus:uppercase focus:tracking-wider focus:text-zinc-100"
        >
          Skip to main content
        </a>
        <AppShell watchlist={<Watchlist />}>{children}</AppShell>
        <Analytics />
      </body>
    </html>
  );
}
