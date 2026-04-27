"""Post-hoc token / latency / call-count summariser (QNT-129).

For each bench alias, queries Langfuse's v2 metrics endpoint for the
``providedModelName`` dimension across the bench window. Sums token
counts and reports per-LLM-call p50 latency, ready to paste into
``docs/model-bench-2026-04.md``.

Run after a bench sweep finishes::

    uv run python scripts/evals/token_summary.py

Caveat -- synthesize gap (QNT-129 finding):
    Synthesize uses ``llm.with_structured_output(Thesis)`` which returns a
    parsed Pydantic model rather than an ``AIMessage``. ``traced_invoke``
    extracts ``providedModelName`` and ``usage_details`` from
    ``AIMessage.response_metadata`` only, so synthesize spans land in
    Langfuse with model=None and tokens=0. The reported total below is
    therefore plan + judge + (synthesize errors that fell back to AIMessage)
    -- a lower bound on actual consumption. Follow-up: thread raw response
    through ``with_structured_output(..., include_raw=True)`` and update
    tracing to read tokens from ``response['raw']``.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import UTC, datetime, timedelta

from langfuse import Langfuse  # type: ignore[import-not-found]

ALIASES = (
    "equity-agent/bench-gptoss120b",
    "equity-agent/bench-gptoss20b",
    "equity-agent/bench-llama4scout",
    "equity-agent/bench-qwen3-32b",
    "equity-agent/bench-gemma4-31b",
    "equity-agent/bench-gemma3-27b",
    "equity-agent/bench-gemini31flashlite",
    "equity-agent/bench-llama3-70b",
)


def query(lf: Langfuse, start: datetime, end: datetime) -> list[dict]:
    q = {
        "view": "observations",
        "dimensions": [{"field": "providedModelName"}],
        "metrics": [
            {"measure": "totalTokens", "aggregation": "sum"},
            {"measure": "inputTokens", "aggregation": "sum"},
            {"measure": "outputTokens", "aggregation": "sum"},
            {"measure": "count", "aggregation": "count"},
            {"measure": "latency", "aggregation": "p50"},
        ],
        "filters": [
            {"column": "type", "operator": "=", "value": "GENERATION", "type": "string"},
        ],
        "fromTimestamp": start.isoformat(),
        "toTimestamp": end.isoformat(),
    }
    return lf.api.metrics.metrics(query=json.dumps(q)).data


def main() -> int:
    end = datetime.now(UTC)
    start = end - timedelta(hours=6)  # widen -- bench runs serially over a few hours
    lf = Langfuse()
    rows = query(lf, start, end)
    by_model = {r["providedModelName"]: r for r in rows if r.get("providedModelName")}
    writer = csv.writer(sys.stdout)
    writer.writerow(
        [
            "alias",
            "n_generations",
            "total_input_tokens",
            "total_output_tokens",
            "total_tokens",
            "p50_call_latency_ms",
        ]
    )
    for alias in ALIASES:
        r = by_model.get(alias)
        if r is None:
            writer.writerow([alias, 0, 0, 0, 0, 0])
            continue
        writer.writerow(
            [
                alias,
                int(r.get("count_count", 0)),
                int(r.get("sum_inputTokens", 0)),
                int(r.get("sum_outputTokens", 0)),
                int(r.get("sum_totalTokens", 0)),
                int(r.get("p50_latency", 0) or 0),
            ]
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
