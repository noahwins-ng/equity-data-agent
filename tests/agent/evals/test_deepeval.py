"""DeepEval LLM-judged generation suite, expressed as pytest cases (QNT-264).

Marked ``deepeval`` so it runs OFF the per-PR hot path: ci.yml's per-PR steps
are ``-m "not integration and not eval"`` (unit) and ``-m eval`` (the
deterministic RAG gate), neither of which collects this marker. The LLM-judged
suite runs only in the nightly / workflow_dispatch job (``-m deepeval``, keys as
job-scoped secrets) and locally. The per-PR RAG gate stays the deterministic one
(``test_retrieval_eval.py``, QNT-261).

Two layers:
* Offline wiring (no stack, no LLM): the metric set, thresholds, history columns,
  judge identity, and custom G-Eval config are internally consistent. These
  catch a drift between the metric keys, the THRESHOLDS map, and the history.csv
  schema without spending a token.
* Live judged case (needs the dev stack + a clean rate-limit window): runs the
  real agent on one sampled record and asserts the RAGAS metrics via DeepEval's
  ``assert_test``, plus the deterministic number-grounding coexistence (AC4).
  Skipped -- never failed -- when the stack is unreachable.
"""

from __future__ import annotations

import pytest
from agent.evals import deepeval_eval as de
from agent.evals.golden_set import HISTORY_FIELDS, run_record
from agent.llm import DEEPEVAL_JUDGE_ALIAS
from shared.tickers import TICKERS

pytestmark = pytest.mark.deepeval


# --- offline wiring (AC1/AC2/AC4/AC5 are internally consistent) -----------------


def test_metric_keys_match_thresholds() -> None:
    """Every built metric has a threshold and vice-versa -- a missing key would
    mean a metric silently ungated or a threshold pointing at nothing.

    Constructing the judge builds a ChatOpenAI client only (no network until a
    metric's ``.measure()``), so this stays offline."""
    metrics = de.build_metrics(de._judge())
    assert set(metrics) == set(de.THRESHOLDS)


def test_ragas_set_and_custom_geval_present() -> None:
    """AC1: the RAGAS set (faithfulness / answer-relevancy / context
    precision+recall) plus at least one custom G-Eval are all implemented."""
    metrics = de.build_metrics(de._judge())
    for required in (
        "faithfulness",
        "answer_relevancy",
        "context_precision",
        "context_recall",
    ):
        assert required in metrics, f"RAGAS metric {required!r} missing"
    geval = metrics["geval"]
    assert geval.name == de.GEVAL_NAME
    assert "verdict" in de.GEVAL_CRITERIA.lower()


def test_history_schema_has_deepeval_columns() -> None:
    """AC5: history.csv carries the deepeval_* aggregate columns, distinct from
    the integer golden-set ``faithfulness`` judge axis."""
    for col in (
        "deepeval_faithfulness",
        "deepeval_answer_relevancy",
        "deepeval_context_precision",
        "deepeval_context_recall",
        "deepeval_geval",
        "deepeval_n",
    ):
        assert col in HISTORY_FIELDS, f"history.csv missing deepeval column {col!r}"
    # The deterministic golden faithfulness axis must still exist alongside it.
    assert "faithfulness" in HISTORY_FIELDS


def test_judge_routes_through_pinned_deepeval_alias() -> None:
    """AC2 / ADR-023: the DeepEval judge is the pinned DeepSeek bench alias on
    OpenRouter (a deliberate paid judge -- removes the free-tier token ceiling so
    a >=50-record baseline runs in one window), NOT the free dialogue judge."""
    got = de.LiteLLMJudge.get_model_name(de.LiteLLMJudge.__new__(de.LiteLLMJudge))
    assert got == DEEPEVAL_JUDGE_ALIAS
    assert "bench" in DEEPEVAL_JUDGE_ALIAS and "deepseek" in DEEPEVAL_JUDGE_ALIAS


def test_aggregate_ignores_nan() -> None:
    """A metric that failed (NaN) must not poison the mean of the ones that
    succeeded -- otherwise one transient judge error blanks the whole run."""
    cases = [
        de.DeepEvalCase("a", "NVDA", {"faithfulness": 0.8}, True, "clean"),
        de.DeepEvalCase("b", "AAPL", {"faithfulness": float("nan")}, True, "clean"),
        de.DeepEvalCase("c", "MSFT", {"faithfulness": 0.6}, True, "clean"),
    ]
    means = de.aggregate(cases)
    assert means["faithfulness"] == pytest.approx(0.7)


def test_history_append_blanks_nan_axis(tmp_path) -> None:
    """AC5: an all-failed axis is recorded blank (not 'nan'), so the committed
    history.csv never carries a literal NaN token that breaks downstream parsers."""
    import csv

    history = tmp_path / "history.csv"
    means = {
        "faithfulness": 0.82,
        "answer_relevancy": float("nan"),
        "context_precision": 0.9,
        "context_recall": 0.77,
        "geval": 0.71,
    }
    de.append_deepeval_history(means, n_cases=4, run_id="testrun", history_path=history)
    row = next(iter(csv.DictReader(history.open())))
    assert row["eval_type"] == "deepeval"
    assert row["deepeval_faithfulness"] == "0.82"
    assert row["deepeval_answer_relevancy"] == ""  # NaN -> blank
    assert row["deepeval_n"] == "4"


# --- AC3/AC4: re-derived floors + aggregate gate (offline) ----------------------

# The recorded baseline means (run 20260625T072005Z-39fc33-deepeval, n=55, judge
# DeepSeek V4 Flash) the floors were derived from. Pinning them here turns a future
# THRESHOLDS edit that crosses the measured baseline into a red test.
_BASELINE_MEANS = {
    "faithfulness": 0.8309,
    "answer_relevancy": 0.8728,
    "context_precision": 0.7029,
    "context_recall": 0.9667,
    "geval": 0.7800,
}


def test_floors_were_rederived_below_baseline() -> None:
    """AC3: every floor sits below its measured-baseline mean (a regression
    tripwire, not an unreachable aspiration) with real margin (~0.05-0.15)."""
    for name, mean in _BASELINE_MEANS.items():
        floor = de.THRESHOLDS[name]
        assert floor < mean, f"{name} floor {floor} >= baseline mean {mean}"
        assert mean - floor <= 0.2, f"{name} floor {floor} too far below mean {mean}"


def test_aggregate_gate_passes_on_baseline() -> None:
    """AC4: the recorded baseline clears every floor -- the enforcement gate is a
    real pass on the calibration run, not a gate nothing can satisfy."""
    assert de.gate_failures(_BASELINE_MEANS) == []


def test_aggregate_gate_catches_regression() -> None:
    """AC4: a mean below a floor (or a NaN/blanked axis) is reported as a failure."""
    bad = {**_BASELINE_MEANS, "context_recall": 0.50}
    failures = de.gate_failures(bad)
    assert any("context_recall" in f for f in failures)
    assert de.gate_failures({**_BASELINE_MEANS, "geval": float("nan")})


def test_enforcement_on_by_default(monkeypatch) -> None:
    """AC4: enforcement is ON by default (floors are trustworthy), opt-out via =0."""
    monkeypatch.delenv("DEEPEVAL_ENFORCE_THRESHOLDS", raising=False)
    assert de.enforcement_enabled() is True
    monkeypatch.setenv("DEEPEVAL_ENFORCE_THRESHOLDS", "0")
    assert de.enforcement_enabled() is False


# --- recall golden set (QNT-275; offline, no stack) -----------------------------


def test_recall_goldens_meet_size_floor() -> None:
    """AC1/AC2: the recall set is >=50 records (the design-doc baseline floor, the
    same statistical floor the retrieval eval enforces). The DeepEval judge runs
    on a paid OpenRouter model so a >=50 run isn't free-tier-token-bound (ADR-023).
    The 55-record set gives full ticker + intent coverage with headroom."""
    records = de.load_recall_goldens()
    assert len(records) >= 50, f"recall set has {len(records)} records, need >=50"


def test_recall_goldens_cover_every_ticker() -> None:
    """Every in-scope ticker appears, so a per-ticker recall regression can't hide
    behind an unrepresented symbol."""
    covered = {r.ticker for r in de.load_recall_goldens()}
    assert covered == set(TICKERS), f"missing tickers: {set(TICKERS) - covered}"


def test_recall_reference_maps_onto_expected_output() -> None:
    """The recall_reference must land on reference_thesis so the existing
    build_test_case feeds it as DeepEval's expected_output unchanged -- a non-empty
    reference is what ContextualRecallMetric attributes against the context."""
    records = de.load_recall_goldens()
    assert all(r.reference_thesis.strip() for r in records)
    # Distinct from the structured goldens: this set is loaded from its own file.
    assert de.RECALL_GOLDENS_PATH.name == "deepeval_recall.yaml"


def test_recall_goldens_reject_unknown_ticker(tmp_path) -> None:
    """A malformed edit (bad ticker) fails loudly rather than silently judging
    fewer records."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "recall:\n  - id: x\n    ticker: ZZZZ\n    question: q\n    recall_reference: r\n"
    )
    with pytest.raises(ValueError, match="unknown ticker"):
        de.load_recall_goldens(bad)


# --- live judged case (needs the dev stack + a clean window) --------------------


def test_live_deepeval_sample_judged() -> None:
    """End-to-end: run the agent on one record and judge it through DeepEval.

    The stack-reachability probe lives in the body, NOT a ``skipif`` decorator:
    a decorator condition is evaluated at import/collection time, which pytest
    does for every file under ``testpaths`` even when ``-m deepeval`` later
    deselects it -- so a decorator would fire two ``httpx.get`` probes on every
    per-PR unit run. In the body it only runs when this test is actually selected.

    Runs the RECALL golden set (QNT-275), not the structured questions.yaml: the
    references here are attributable to the gathered reports, so context_recall is
    meaningful rather than the 0.29 shape-reference artifact.

    Soft by default, like the golden judge (``EVAL_MIN_JUDGE`` off until history
    earns a trustworthy number): the metric scores are a recorded signal, and the
    suite asserts the things that ARE contracts -- every RAGAS axis produced a
    real score, and the deterministic number-grounding layer ran additively. The
    threshold gate via DeepEval's canonical ``assert_test`` is opt-in behind
    ``DEEPEVAL_ENFORCE_THRESHOLDS`` -- QNT-275 enables it once a clean >=50-record
    baseline re-derives the floors (THRESHOLDS). The judge runs on a paid
    OpenRouter model (ADR-023), so the >=50 baseline isn't free-tier-token-bound."""
    if not de.stack_reachable():
        pytest.skip(
            "dev stack unreachable (need make dev-litellm / dev-api / tunnel) -- "
            "live DeepEval judging skipped, not failed"
        )

    record = de.load_recall_goldens()[0]
    outcome = run_record(record)
    if outcome.provider_error or not outcome.thesis:
        pytest.skip("provider error / empty thesis -- contaminated window, re-run")

    judge = de._judge()
    metrics = de.build_metrics(judge)
    case = de.score_outcome(outcome, metrics)

    # The deterministic number-grounding faithfulness layer coexists.
    assert isinstance(case.grounding_ok, bool)
    # Every RAGAS axis produced a real (non-NaN) score on a clean window.
    for name in de.THRESHOLDS:
        score = case.scores.get(name, float("nan"))
        assert score == score, f"metric {name} did not produce a score"
    # AC4 enforcement is the AGGREGATE gate (de.gate_failures in the CLI main),
    # NOT a per-record assert_test here: a single record's context_precision can
    # dip to ~0.5 (LLM-judge variance) and would false-fail the floor, so this
    # smoke test asserts only that the judged path PRODUCES scores. The real
    # pass/fail runs over the >=50-record mean via `python -m agent.evals.deepeval_eval`.
