import type { Metadata } from "next";
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
        <AppShell watchlist={<Watchlist />}>{children}</AppShell>
        <Analytics />
      </body>
    </html>
  );
}
