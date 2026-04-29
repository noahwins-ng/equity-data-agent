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
    The asset-required keys are maintained as an explicit list (rather than
    grepped at test time) because not every Settings field is read inside
    a launcher-spawned container — agent + api run elsewhere.

DAGSTER_HOME is on an explicit allowlist — it's Dagster infra, not an app
Setting.
"""

from pathlib import Path

import yaml
from shared.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
DAGSTER_YAML = REPO_ROOT / "dagster.yaml"

INFRA_ALLOWLIST = frozenset({"DAGSTER_HOME"})

# Settings fields that asset code in packages/dagster-pipelines/ reads directly
# (`settings.X`) and therefore MUST appear in env_vars for the spawned per-run
# container to see them. Maintain this list when adding a new
# `from shared.config import settings` reference inside dagster-pipelines.
# Keep this in sync with grep "settings\." packages/dagster-pipelines/src/.
ASSET_REQUIRED_SETTINGS = frozenset(
    {
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_PORT",
        "FINNHUB_API_KEY",
        "QDRANT_API_KEY",
        "QDRANT_URL",
    }
)


def _load_env_vars() -> list[str]:
    with DAGSTER_YAML.open() as f:
        config = yaml.safe_load(f)
    return config["run_launcher"]["config"]["env_vars"]


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


def test_asset_required_settings_subset_of_env_vars() -> None:
    """Inverse direction (QNT-144): every Settings key read inside a
    DockerRunLauncher-spawned container must appear in env_vars, or asset
    code receives an empty value at runtime.
    """
    env_vars = set(_load_env_vars())
    settings_fields = set(Settings.model_fields.keys())

    unknown_required = ASSET_REQUIRED_SETTINGS - settings_fields
    assert not unknown_required, (
        f"ASSET_REQUIRED_SETTINGS lists keys that are not in Settings: "
        f"{sorted(unknown_required)}. The list has drifted from "
        f"shared.config.Settings — update one or the other."
    )

    missing = ASSET_REQUIRED_SETTINGS - env_vars
    assert not missing, (
        f"dagster.yaml run_launcher.config.env_vars is missing keys that "
        f"asset code in packages/dagster-pipelines/ reads from Settings: "
        f"{sorted(missing)}. DockerRunLauncher will not forward them into "
        f"per-run containers, so settings.<key> will be empty at runtime "
        f"and the asset will fail (QNT-144)."
    )
