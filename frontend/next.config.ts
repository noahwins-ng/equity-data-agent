import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // QNT-424: give the build-time retry ladder in
  // `app/ticker/[symbol]/page.tsx` room to actually run. Each attempt is
  // capped at BUILD_FETCH_TIMEOUT_MS (8 s) and the backoff sums to 30 s, so a
  // fully hung API costs ~70 s per page — over Next's 60 s default, which
  // would kill the render mid-ladder and make the retry policy decorative.
  // 120 s leaves headroom without letting a genuinely wedged build hang long.
  // If you change the ladder, re-derive this number (and vice versa).
  staticPageGenerationTimeout: 120,
};

export default nextConfig;
