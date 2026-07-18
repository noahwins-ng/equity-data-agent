"""CI guard: dagster.yaml run_launcher env_vars contract is bidirectional.

The `run_launcher.config.env_vars` list in dagster.yaml is the passthrough
into per-run containers spawned by DockerRunLauncher. Two ways it can drift,
both silent in prod:

QNT-125 direction (env_vars contains a key Settings does not have):
    Listing a key that the daemon's environment does not set causes the
    launcher to drop the run at dequeue ("Tried to load environment variable
    X, but it was not set"). QNT-59 removed OLLAMA_API_KEY and ANTHROPIC_API_KEY
    from Settings but left both in env_vars; the launcher dropped every queued
    run for ~20h before detection.

QNT-144 direction (Settings has a key env_vars does not list):
    A Settings field that asset code reads (settings.FINNHUB_API_KEY etc.)
    is empty inside the spawned container if env_vars omits it. QNT-141
    added FINNHUB_API_KEY to Settings + .env + SOPS but skipped env_vars;
    60 queued news_raw runs were silently dropped over ~24h before detection.

QNT-381 follow-up: the required-keys list used to be hand-maintained, which
shares the human-memory failure mode that caused QNT-59/125, QNT-141/144,
and the QNT-381 near-miss (ClickHouse creds wired into Settings + resource
but not env_vars — caught in review, would have auth-failed every prod
run-worker). It is now DERIVED at test time by grepping `settings.X`
references under packages/dagster-pipelines/src/. A new reference must be
classified — into env_vars, DAEMON_CONTEXT_SETTINGS, or
DEFAULTS_ONLY_SETTINGS — or this suite fails with instructions. The failure
mode flips from silently-dropped prod runs to a red CI check.

DAGSTER_HOME is on an explicit allowlist — it's Dagster infra, not an app
Setting.
"""

import re
from pathlib import Path

import yaml
from shared.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
DAGSTER_YAML = REPO_ROOT / "dagster.yaml"
PIPELINES_SRC = REPO_ROOT / "packages" / "dagster-pipelines" / "src"

INFRA_ALLOWLIST = frozenset({"DAGSTER_HOME"})

_SETTINGS_REF = re.compile(r"\bsettings\.([A-Z][A-Z0-9_]*)\b")

# Fields referenced only from sensor/schedule evaluation code, which runs in
# dagster-code-server / dagster-daemon (both have `env_file: .env`) — never
# inside a DockerRunLauncher-spawned run container. They must NOT be forced
# into env_vars: if prod .env lacks the key, the launcher drops every run at
# dequeue (QNT-125 direction).
DAEMON_CONTEXT_SETTINGS = frozenset(
    {
        # dagster_run_failure_alert_sensor (@run_failure_sensor — daemon).
        "DAGSTER_BASE_URL",
        "DISCORD_WEBHOOK_URL",
    }
)

# Fields read inside run-worker context but DELIBERATELY not passed through:
# run-workers fall back to the code defaults in shared.config.Settings, and
# prod .env carries no lines for them (verified against .env.sops at the time
# of writing) — so listing them in env_vars would drop runs at dequeue
# (QNT-125), and passing an empty .env value through would OVERRIDE a good
# code default with "" (e.g. SEC_EDGAR_USER_AGENT= would blank the User-Agent
# SEC requires). To make one of these operator-tunable in prod, move it out
# of this set AND add it to env_vars AND add a non-empty line to .env.sops in
# the same change.
DEFAULTS_ONLY_SETTINGS = frozenset(
    {
        # earnings_embeddings asset (contextual-embedding switches).
        "CONTEXT_MAX_DOC_CHARS",
        "CONTEXT_MODEL",
        "CONTEXT_THROTTLE_SECONDS",
        "EARNINGS_CONTEXTUAL",
        # run_online_eval @op — launched via online_eval_job → run-worker.
        "ONLINE_EVAL_SAMPLE_RATE",
        # edgar_feeds fetch path, called from the earnings ingest asset.
        "SEC_EDGAR_USER_AGENT",
    }
)


def _load_env_vars() -> list[str]:
    with DAGSTER_YAML.open() as f:
        config = yaml.safe_load(f)
    return config["run_launcher"]["config"]["env_vars"]


def _referenced_settings() -> set[str]:
    """Every Settings field referenced as `settings.X` under dagster-pipelines."""
    refs: set[str] = set()
    for path in sorted(PIPELINES_SRC.rglob("*.py")):
        refs.update(_SETTINGS_REF.findall(path.read_text(encoding="utf-8")))
    # Docstrings/comments can mention fields that aren't real (regex is
    # source-text-level); intersect with actual Settings fields.
    return refs & set(Settings.model_fields.keys())


def test_env_vars_subset_of_settings_or_allowlist() -> None:
    env_vars = set(_load_env_vars())
    settings_fields = set(Settings.model_fields.keys())
    allowed = settings_fields | INFRA_ALLOWLIST

    unknown = env_vars - allowed
    assert not unknown, (
        f"dagster.yaml run_launcher.config.env_vars contains keys that are "
        f"neither Settings fields nor on the infra allowlist: {sorted(unknown)}. "
        f"Either add the field to shared.config.Settings, add it to "
        f"INFRA_ALLOWLIST in this test, or remove it from dagster.yaml. "
        f"Leaving an undefined key there causes DockerRunLauncher to drop "
        f"every run at dequeue (QNT-125)."
    )


def test_env_vars_has_no_duplicates() -> None:
    env_vars = _load_env_vars()
    assert len(env_vars) == len(set(env_vars)), (
        f"dagster.yaml run_launcher.config.env_vars contains duplicate keys: "
        f"{sorted(k for k in set(env_vars) if env_vars.count(k) > 1)}"
    )


def test_every_referenced_setting_is_classified() -> None:
    """Auto-derived QNT-144 direction: every `settings.X` reference under
    packages/dagster-pipelines/ must be in env_vars, or explicitly classified
    as daemon-context or defaults-only. A new unclassified reference fails
    here instead of silently yielding a default/empty value in prod
    run-workers (QNT-141/144, QNT-381 near-miss).
    """
    env_vars = set(_load_env_vars())
    referenced = _referenced_settings()

    unclassified = referenced - env_vars - DAEMON_CONTEXT_SETTINGS - DEFAULTS_ONLY_SETTINGS
    assert not unclassified, (
        f"settings fields referenced in packages/dagster-pipelines/ but not "
        f"classified: {sorted(unclassified)}. Decide where each one runs:\n"
        f"  * read inside an asset/op (run-worker) and operator-tunable → add "
        f"to dagster.yaml env_vars AND ensure prod .env carries the key\n"
        f"  * read only in sensor/schedule evaluation (daemon/code-server) → "
        f"add to DAEMON_CONTEXT_SETTINGS\n"
        f"  * run-worker code that should just use the Settings default → add "
        f"to DEFAULTS_ONLY_SETTINGS\n"
        f"Leaving it unclassified risks the QNT-141/144 silent-default failure."
    )


def test_classification_lists_are_not_stale() -> None:
    """The two exception lists must stay minimal: entries must still be
    referenced in source, and must not also appear in env_vars (an entry in
    both would mean the exception no longer applies).
    """
    env_vars = set(_load_env_vars())
    referenced = _referenced_settings()

    stale = (DAEMON_CONTEXT_SETTINGS | DEFAULTS_ONLY_SETTINGS) - referenced
    assert not stale, (
        f"classification lists contain fields no longer referenced under "
        f"packages/dagster-pipelines/: {sorted(stale)}. Remove them."
    )

    contradictions = (DAEMON_CONTEXT_SETTINGS | DEFAULTS_ONLY_SETTINGS) & env_vars
    assert not contradictions, (
        f"fields classified as exceptions but also present in env_vars: "
        f"{sorted(contradictions)}. Remove them from the exception list (they "
        f"are passed through) or from env_vars (they should not be)."
    )

    overlap = DAEMON_CONTEXT_SETTINGS & DEFAULTS_ONLY_SETTINGS
    assert not overlap, f"fields in both exception lists: {sorted(overlap)}"
