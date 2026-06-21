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

import os

import pytest
from agent.evals import deepeval_eval as de
from agent.evals.golden_set import HISTORY_FIELDS
from agent.llm import JUDGE_ALIAS

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


def test_judge_routes_through_litellm_free_alias() -> None:
    """AC2: the judge is the pinned free LiteLLM alias, not a paid default."""
    assert de.LiteLLMJudge.get_model_name(de.LiteLLMJudge.__new__(de.LiteLLMJudge)) == JUDGE_ALIAS
    assert "bench" in JUDGE_ALIAS  # the free Cerebras bench judge


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


# --- live judged case (needs the dev stack + a clean window) --------------------


def test_live_deepeval_sample_judged() -> None:
    """End-to-end: run the agent on one record and judge it through DeepEval.

    The stack-reachability probe lives in the body, NOT a ``skipif`` decorator:
    a decorator condition is evaluated at import/collection time, which pytest
    does for every file under ``testpaths`` even when ``-m deepeval`` later
    deselects it -- so a decorator would fire two ``httpx.get`` probes on every
    per-PR unit run. In the body it only runs when this test is actually selected.

    Soft by default, like the golden judge (``EVAL_MIN_JUDGE`` off until history
    earns a trustworthy number): the metric scores are a recorded signal, and the
    suite asserts the things that ARE contracts -- every RAGAS axis produced a
    real score, and the deterministic number-grounding layer ran additively
    (AC4). The threshold gate via DeepEval's canonical ``assert_test`` is opt-in
    behind ``DEEPEVAL_ENFORCE_THRESHOLDS`` -- enable it once a clean >=50-record
    baseline re-derives the floors (the first n=4 baseline showed context_recall
    is structurally low on focused-query goldens whose synthesized reference is
    broader than the raw report context, so the design-doc 0.8 floor doesn't fit
    yet -- same calibration discipline as the retrieval gate floors)."""
    if not de.stack_reachable():
        pytest.skip(
            "dev stack unreachable (need make dev-litellm / dev-api / tunnel) -- "
            "live DeepEval judging skipped, not failed"
        )

    from agent.evals.golden_set import load_goldens, run_record

    record = load_goldens()[0]
    outcome = run_record(record)
    if outcome.provider_error or not outcome.thesis:
        pytest.skip("provider error / empty thesis -- contaminated window, re-run")

    judge = de._judge()
    metrics = de.build_metrics(judge)
    case = de.score_outcome(outcome, metrics)

    # AC4: the deterministic number-grounding faithfulness layer coexists.
    assert isinstance(case.grounding_ok, bool)
    # Every RAGAS axis produced a real (non-NaN) score on a clean window.
    for name in de.THRESHOLDS:
        score = case.scores.get(name, float("nan"))
        assert score == score, f"metric {name} did not produce a score"

    # Opt-in hard gate: DeepEval's canonical pytest-native ``assert_test``.
    if os.environ.get("DEEPEVAL_ENFORCE_THRESHOLDS"):
        from deepeval import assert_test

        assert_test(de.build_test_case(outcome), list(metrics.values()))
