"""Regression tests for Dagster definitions loading.

The Apr-29 QNT-134 deploy passed our `defs.resolve_asset_graph()` hard gate
but still landed a broken code location in prod: `ohlcv_downstream_job` had
been written to select assets with mismatched partition definitions
(`ohlcv_weekly`/`ohlcv_monthly` partitioned over 11 tickers; indicators +
`fundamental_summary` over 10). Dagster requires every asset selected in one
job to share a partition def; the resolver only fails when jobs are
*resolved*, which `resolve_asset_graph()` does not do — it just walks asset
keys. The code-server then tripped on the unresolved job at startup.

These tests force every registered job to resolve at unit-test time so the
same class of failure can't slip through CI again.
"""

from __future__ import annotations

import pytest
from dagster_pipelines.definitions import defs


@pytest.mark.parametrize("job_name", [job.name for job in (defs.jobs or [])])
def test_every_job_resolves(job_name: str) -> None:
    """Every job in `defs.jobs` must resolve cleanly.

    `defs.get_job_def(name)` triggers the same resolver path the code-server
    runs at boot. If it raises, the deploy would land a broken code location.
    """
    job = defs.get_job_def(job_name)
    assert job.name == job_name
