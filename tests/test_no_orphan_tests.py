"""CI guard: no test_*.py files may live under packages/*/tests/.

QNT-127: ``pyproject.toml`` pins ``[tool.pytest.ini_options] testpaths =
["tests"]``, so pytest only walks the repo-root ``tests/`` tree. Any test
file dropped into ``packages/<pkg>/tests/`` is silently skipped by
``uv run pytest`` and by CI — ~1,340 lines of test code never ran for
months before QNT-127 surfaced the drift (including ``test_health.py``'s
deploy-identity contract from QNT-51).

This guard puts the bug in a trap: if a new test accidentally lands under
``packages/*/tests/`` (muscle memory, IDE default, copy-paste from an old
sibling), CI fails with a clear migration instruction instead of going
silently uncovered.

Symmetric to ``tests/dagster/test_run_launcher_env_vars.py`` (QNT-125),
which traps a similar drift between ``dagster.yaml`` and
``shared.config.Settings``.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES_DIR = REPO_ROOT / "packages"


def test_no_test_files_under_packages_tests_dirs() -> None:
    # Cover pytest's two default discovery patterns (test_*.py, *_test.py) at
    # any depth under packages/<pkg>/tests/, so nested subdirs can't sneak past.
    offenders = sorted(
        {
            *PACKAGES_DIR.glob("*/tests/**/test_*.py"),
            *PACKAGES_DIR.glob("*/tests/**/*_test.py"),
        }
    )
    assert not offenders, (
        "Found test_*.py files under packages/*/tests/ — these are silently "
        "skipped by pytest (testpaths=['tests'] in pyproject.toml) and never "
        "run in CI. Move them into the matching tests/<area>/ directory at "
        "the repo root:\n"
        + "\n".join(f"  {p.relative_to(REPO_ROOT)}" for p in offenders)
        + "\n\nSee QNT-127 for the migration that cleaned this up the first time."
    )
