// Run with: `npm test` (from `frontend/`) — Node's built-in TS loader + node:test.

import test from "node:test";
import assert from "node:assert/strict";

import { formatNewsDate } from "./format.ts";

// ─── QNT-252: news date is absolute-only (no build-time relative bucket) ───
//
// NewsCard is baked into the statically built ticker page, so a relative
// "Today / Yesterday / 2d ago" bucket computed at build time would freeze and
// drift between deploys. The label must therefore be absolute-only — these
// assert it carries no relative prefix regardless of how old the article is.

test("renders the absolute date with no relative prefix", () => {
  assert.equal(formatNewsDate("2026-04-28T12:00:00Z"), "Apr 28");
});

test("a stale article is never labeled Today / Yesterday / Nd ago", () => {
  // Whatever 'now' is at view time, the output has no relative bucket — so an
  // article that was 'Today' at build time cannot still read 'Today' later.
  for (const iso of ["2026-04-28T12:00:00Z", "2020-01-01T00:00:00Z", new Date().toISOString()]) {
    const label = formatNewsDate(iso);
    assert.doesNotMatch(label, /Today|Yesterday|ago|·/);
  }
});

test("missing or invalid date renders the em-dash placeholder", () => {
  assert.equal(formatNewsDate(null), "—");
  assert.equal(formatNewsDate("not-a-date"), "—");
});
