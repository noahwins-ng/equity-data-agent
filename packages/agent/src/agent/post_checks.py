"""Deterministic post-graph assertions: verdict-action direction check (QNT-193).

After synthesize populates Thesis.verdict_action, this module checks that any
price levels quoted in the action text are directionally consistent with the
current close. A "target" or "resistance" level below close is already
exceeded; a "support" or "defend" level above close is already broken.

The check is intentionally narrow: it only fires on Thesis responses (the only
shape with a verdict_action today) and only when the technical report supplies a
parseable Close: line. All other cases return (True, "ok") / "skipped".

Integration:
  eval_scores.push_to_trace_id() calls push_verdict_direction_score() to add a
  "verdict_direction_ok" Langfuse score for every thesis trace.

  A module-level rolling-window counter escalates to Discord via
  Settings.DISCORD_WEBHOOK_URL when >= _ESCALATE_THRESHOLD mismatches occur in
  _ESCALATE_WINDOW_SECS — the same webhook seam used by docker-events-notify.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from collections import deque
from typing import TYPE_CHECKING

from shared.config import Settings

if TYPE_CHECKING:
    from agent.thesis import Thesis

logger = logging.getLogger(__name__)

# ── Framing-word patterns ───────────────────────────────────────────────────

# These framing words imply the associated level should be ABOVE the current
# close (a target or resistance the price has not yet reached).
_UPSIDE_RE = re.compile(r"\b(target|resistance)\b", re.IGNORECASE)

# These framing words imply the associated level should be BELOW the current
# close (a support the price is trading above).
_DOWNSIDE_RE = re.compile(r"\b(support|defend|hold|floor)\b", re.IGNORECASE)

# ── Extraction patterns ─────────────────────────────────────────────────────

# Price-level candidates: 1–6 digits, optional thousand-comma, optional 1–2
# decimal places. Word-boundary anchors prevent matching mid-number.
_LEVEL_RE = re.compile(r"(?<!\w)(\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?)(?!\w)")

# Close line from the technical report: "Close: 225.32 (...)"
_CLOSE_RE = re.compile(r"^Close:\s*([\d,]+(?:\.\d{1,2})?)", re.MULTILINE)

# Clause delimiters: semicolons, period-space, and comma-space sentence boundaries.
# "comma-space" splits prose clauses without touching thousand-separators (1,234.56
# has no space after the comma, so the regex leaves it intact).
_CLAUSE_RE = re.compile(r";|,\s+|\.\s+")

# ── Discord escalation state ────────────────────────────────────────────────

_ESCALATE_THRESHOLD = 3
_ESCALATE_WINDOW_SECS = 3600  # 1 hour

_mismatch_timestamps: deque[float] = deque()
_settings = Settings()


# ── Core helpers ────────────────────────────────────────────────────────────


def _parse_float(s: str) -> float:
    return float(s.replace(",", ""))


def _extract_close(technical_report: str) -> float | None:
    """Parse the close price from the technical report's Close: header line."""
    m = _CLOSE_RE.search(technical_report)
    return _parse_float(m.group(1)) if m else None


def _level_contexts(verdict_action: str, close: float) -> list[tuple[str, float, str]]:
    """Return (raw_str, level, clause) triples for price-like numbers in the action text.

    Each level is paired with the clause it appears in (split on `;` and `.` sentence
    boundaries). Framing-word checks compare against the clause, not an arbitrary
    character window, so neighbouring clauses cannot bleed framing words into each
    other (e.g. "Trim above 250; defend SMA-50 at 193" must not flag 250 for "defend").

    Filters out:
    - Numbers preceded by '-' (SMA-50 → '50' is a period, not a price)
    - Numbers that are less than 10% of close (likely RSI/MACD values, not prices)
    """
    results = []
    for clause in _CLAUSE_RE.split(verdict_action):
        for m in _LEVEL_RE.finditer(clause):
            # Skip indicator-period numbers attached via hyphen (e.g. SMA-50 → 50)
            if m.start() > 0 and clause[m.start() - 1] == "-":
                continue
            raw = m.group(1)
            try:
                level = _parse_float(raw)
            except ValueError:
                continue
            # Exclude values that look like RSI/MACD/BB parameters rather than prices
            if level < close * 0.1:
                continue
            results.append((raw, level, clause))
    return results


# ── Public API ───────────────────────────────────────────────────────────────


def check_verdict_direction(thesis: Thesis, technical_report: str) -> tuple[bool, str]:
    """Check that price levels in verdict_action are directionally consistent with close.

    Returns (ok, comment):
      ok=True   — no directional mismatch found (or check was skipped).
      ok=False  — one or more levels are inconsistent; comment names each bad level.

    A target/resistance level below close is already exceeded (upside mismatch).
    A support/defend level above close is already broken (downside mismatch).
    """
    close = _extract_close(technical_report)
    if close is None:
        return True, "no close in technical report -- skipped"

    bad: list[str] = []
    for raw, level, clause in _level_contexts(thesis.verdict_action, close):
        if level < close and _UPSIDE_RE.search(clause):
            bad.append(f"{raw} (upside framing, already below close {close})")
        elif level > close and _DOWNSIDE_RE.search(clause):
            bad.append(f"{raw} (downside framing, already above close {close})")

    if bad:
        return False, f"direction mismatch: {'; '.join(bad)}"
    return True, "ok"


def _fire_discord_alert(count: int) -> None:
    """POST a Discord notification when the mismatch threshold is exceeded.

    Silent no-op when DISCORD_WEBHOOK_URL is unset (dev / eval bench runs).
    Failures are logged at WARNING and swallowed — alerting must not crash the
    request path.
    """
    url = _settings.DISCORD_WEBHOOK_URL
    if not url:
        return
    body = json.dumps(
        {
            "content": (
                f":warning: **verdict_direction_ok**: {count} mismatches in 1h "
                "-- likely prompt regression or technical-report shape drift. "
                "Check agent logs (`make monitor-log`) and review recent thesis outputs."
            )
        }
    ).encode()
    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)  # noqa: S310 — URL comes from Settings, not user input
        logger.info("verdict_direction_ok: Discord alert posted (%d mismatches)", count)
    except Exception as exc:  # noqa: BLE001 — alerting must not crash the caller
        logger.warning("verdict_direction_ok: Discord alert failed: %s", exc)


def record_mismatch() -> None:
    """Record a mismatch event and escalate to Discord if the rolling threshold is hit.

    Maintains a module-level deque of timestamps. When >= _ESCALATE_THRESHOLD
    events fall within _ESCALATE_WINDOW_SECS the Discord webhook is fired and
    the window is cleared to prevent repeated alerts per burst.
    """
    now = time.monotonic()
    _mismatch_timestamps.append(now)

    # Prune events outside the rolling window before counting
    cutoff = now - _ESCALATE_WINDOW_SECS
    while _mismatch_timestamps and _mismatch_timestamps[0] < cutoff:
        _mismatch_timestamps.popleft()

    count = len(_mismatch_timestamps)
    if count >= _ESCALATE_THRESHOLD:
        logger.warning(
            "verdict_direction_ok: %d mismatches in 1h — firing Discord alert",
            count,
        )
        _fire_discord_alert(count)
        _mismatch_timestamps.clear()  # reset after alert to avoid re-firing every subsequent event


__all__ = ["check_verdict_direction", "record_mismatch"]
