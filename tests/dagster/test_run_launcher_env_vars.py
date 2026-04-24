"""CI guard: dagster.yaml run_launcher env_vars must subset Settings.

QNT-125: the `run_launcher.config.env_vars` list in dagster.yaml is a
passthrough contract — every key listed there must exist in the daemon's
environment at run-launch time, or DockerRunLauncher drops the run at
dequeue (fails fast with "Tried to load environment variable X, but it was
not set"). The daemon sources its environment from `.env` via the compose
env_file, which in turn sources its shape from
`packages/shared/src/shared/config.py::Settings`.

Before this guard, the list drifted from Settings (QNT-59 removed
OLLAMA_API_KEY and ANTHROPIC_API_KEY from Settings but left both in
env_vars), which dropped every queued run for ~20h before detection.
This test fails loud on re-drift.

DAGSTER_HOME is on an explicit allowlist — it's Dagster infra, not an app
Setting.
"""

from pathlib import Path

import yaml
from shared.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
DAGSTER_YAML = REPO_ROOT / "dagster.yaml"

INFRA_ALLOWLIST = frozenset({"DAGSTER_HOME"})


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
