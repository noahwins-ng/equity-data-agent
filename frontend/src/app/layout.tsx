import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Analytics } from "@vercel/analytics/next";
import { ChatPanel } from "@/components/chat-panel";
import { MobileNav } from "@/components/mobile-chat-toggle";
import { WatchlistDrawer } from "@/components/watchlist-drawer";
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
        <MobileNav watchlist={<Watchlist />} />
        <WatchlistDrawer watchlist={<Watchlist />} />
        <div className="grid min-h-0 flex-1 grid-cols-1 overflow-hidden md:grid-cols-[minmax(0,1fr)_clamp(18rem,30%,22rem)] lg:grid-cols-[minmax(0,1fr)_clamp(22rem,28%,26rem)] xl:grid-cols-[17rem_minmax(0,1fr)_clamp(22rem,26%,28rem)]">
          <div className="hidden xl:block">
            <Watchlist />
          </div>
          <main className="min-h-0 overflow-y-auto">{children}</main>
          <div className="hidden md:flex md:min-h-0 md:flex-col">
            <ChatPanel />
          </div>
        </div>
        <Analytics />
      </body>
    </html>
  );
}
