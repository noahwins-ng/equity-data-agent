"""Tests for scripts/obs_smoke.py — the QNT-172 pre-prod observability gate.

Covers the four pure parsers/checkers:
  * extract_panel_queries — every dashboard panel's Prom expr is harvested.
  * extract_alert_exprs — every alert rule's underlying PromQL is harvested.
  * extract_compose_bind_mounts / parse_deploy_restart_directives /
    check_cd_restart_wiring — the structural wiring assertion.

The HTTP-bound checks (Prom targets, panel non-empty results, alert data
existence) are exercised in CD against the real running stack — not unit-
tested here because that's exactly the failure mode this ticket exists to
catch (mocked behavior diverging from prod).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Load scripts/obs_smoke.py as a module. Register in sys.modules before
# exec_module: @dataclass on Python 3.14+ resolves the owning module via
# sys.modules during decoration, and raises AttributeError otherwise.
_spec = importlib.util.spec_from_file_location("obs_smoke", REPO_ROOT / "scripts" / "obs_smoke.py")
assert _spec and _spec.loader
obs_smoke = importlib.util.module_from_spec(_spec)
sys.modules["obs_smoke"] = obs_smoke
_spec.loader.exec_module(obs_smoke)


# ─── Panel extraction ───────────────────────────────────────────────────────


def test_extract_panel_queries_walks_repo_dashboards() -> None:
    queries = obs_smoke.extract_panel_queries(
        REPO_ROOT / "observability" / "grafana" / "dashboards"
    )
    # host-overview has 8 panels (each one Prometheus target),
    # containers-overview has 5 panels (one with two targets).
    # Don't pin exact count — just assert non-trivial coverage and that the
    # known-historically-broken queries are present.
    assert len(queries) >= 10, f"got only {len(queries)} panel queries"
    exprs = [q[2] for q in queries]
    # PR #197 regression: cAdvisor without --docker_only → no `name` label.
    assert any("container_memory_working_set_bytes" in e for e in exprs)
    # PR #200 regression: node_exporter mountpoint="/rootfs" → NoData.
    assert any('mountpoint="/"' in e for e in exprs)


def test_extract_panel_queries_skips_non_prometheus_targets(tmp_path: Path) -> None:
    dash = {
        "uid": "mixed",
        "panels": [
            {
                "type": "stat",
                "title": "prom panel",
                "targets": [{"refId": "A", "expr": "up", "datasource": {"type": "prometheus"}}],
            },
            {
                "type": "stat",
                "title": "loki panel",
                "datasource": {"type": "loki"},
                "targets": [{"refId": "A", "expr": '{job="x"}'}],
            },
            {"type": "row", "title": "section", "panels": []},
            {"type": "text", "title": "note"},
        ],
    }
    (tmp_path / "dash.json").write_text(json.dumps(dash))
    qs = obs_smoke.extract_panel_queries(tmp_path)
    assert qs == [("mixed", "prom panel", "up")]


def test_extract_panel_queries_descends_into_row_subpanels(tmp_path: Path) -> None:
    dash = {
        "uid": "rowed",
        "panels": [
            {
                "type": "row",
                "title": "Group A",
                "panels": [
                    {
                        "type": "stat",
                        "title": "nested",
                        "targets": [
                            {
                                "refId": "A",
                                "expr": "node_load1",
                                "datasource": {"type": "prometheus"},
                            }
                        ],
                    }
                ],
            }
        ],
    }
    (tmp_path / "d.json").write_text(json.dumps(dash))
    qs = obs_smoke.extract_panel_queries(tmp_path)
    assert qs == [("rowed", "nested", "node_load1")]


# ─── Alert extraction ───────────────────────────────────────────────────────


def test_extract_alert_exprs_pulls_prom_expr_per_rule() -> None:
    exprs = obs_smoke.extract_alert_exprs(
        REPO_ROOT / "observability" / "grafana" / "provisioning" / "alerting" / "rules.yml"
    )
    titles = {t for t, _ in exprs}
    # All four QNT-103 rules must contribute one expr each.
    assert titles >= {
        "ContainerMemoryHigh",
        "HostMemoryHigh",
        "ContainerRestartLoop",
        "HostDiskHigh",
    }
    # PR #200 regression — host_disk_high must use mountpoint="/", not /rootfs.
    disk_expr = dict(exprs)["HostDiskHigh"]
    assert 'mountpoint="/"' in disk_expr
    assert "/rootfs" not in disk_expr


def test_extract_alert_exprs_skips_math_steps(tmp_path: Path) -> None:
    rules = {
        "groups": [
            {
                "rules": [
                    {
                        "title": "OnlyMath",
                        "data": [{"datasourceUid": "__expr__", "model": {"expression": "A"}}],
                    },
                    {
                        "title": "Real",
                        "data": [
                            {
                                "datasourceUid": "prometheus",
                                "model": {"expr": 'up{job="x"}'},
                            },
                            {"datasourceUid": "__expr__", "model": {"expression": "A"}},
                        ],
                    },
                ]
            }
        ]
    }
    f = tmp_path / "rules.yml"
    f.write_text(yaml.safe_dump(rules))
    exprs = obs_smoke.extract_alert_exprs(f)
    assert exprs == [("Real", 'up{job="x"}')]


# ─── Compose mount extraction ───────────────────────────────────────────────


def test_extract_compose_bind_mounts_skips_named_volumes_and_host_paths() -> None:
    mounts = obs_smoke.extract_compose_bind_mounts(REPO_ROOT / "docker-compose.yml")
    sources = {m.source for m in mounts}
    # Sanity — known config files appear.
    assert "litellm_config.yaml" in sources
    assert "dagster.yaml" in sources
    assert "workspace.yaml" in sources
    assert "observability/prometheus/prometheus.yml" in sources
    assert "observability/grafana/provisioning" in sources
    assert "observability/grafana/dashboards" in sources
    # cAdvisor's runtime mounts (`/`, `/sys`, `/var/run`) are absolute, not
    # repo-relative — they must be filtered out.
    for m in mounts:
        assert not m.source.startswith("/"), m


def test_extract_compose_bind_mounts_skips_dormant_profiles() -> None:
    mounts = obs_smoke.extract_compose_bind_mounts(REPO_ROOT / "docker-compose.yml")
    services = {m.service for m in mounts}
    # caddy is in the dormant `prod-caddy` profile (QNT-75 / ADR-018) — its
    # Caddyfile mount must NOT appear as a covered-by-deploy expectation.
    assert "caddy" not in services


# ─── Deploy directive parsing ───────────────────────────────────────────────


def test_parse_deploy_restart_directives() -> None:
    dirs = obs_smoke.parse_deploy_restart_directives(
        REPO_ROOT / ".github" / "workflows" / "deploy.yml"
    )
    by_source = {(kind, src): svcs for kind, src, svcs in dirs}
    # Exact-match entries we know exist.
    assert by_source[("exact", "litellm_config.yaml")] == ["litellm"]
    assert "dagster" in by_source[("exact", "dagster.yaml")]
    assert "dagster-code-server" in by_source[("exact", "dagster.yaml")]
    assert "dagster-daemon" in by_source[("exact", "dagster.yaml")]
    # Prefix entries (dir-rooted bind mounts).
    assert by_source[("prefix", "observability/grafana/")] == ["grafana"]
    assert by_source[("prefix", "observability/prometheus/")] == ["prometheus"]


# ─── Wiring assertion ───────────────────────────────────────────────────────


def test_check_cd_restart_wiring_passes_against_repo() -> None:
    """The repo's compose ↔ deploy.yml wiring must be self-consistent today."""
    result = obs_smoke.check_cd_restart_wiring(
        REPO_ROOT / "docker-compose.yml",
        REPO_ROOT / ".github" / "workflows" / "deploy.yml",
    )
    assert not result.failures, "\n".join(result.failures)
    assert len(result.passes) >= 9  # at least the 9 known mounts in active profiles


def test_check_cd_restart_wiring_catches_missing_restart(tmp_path: Path) -> None:
    """Synthetic regression: a service mounts a config but deploy.yml forgets it.

    This is the structural bug PR #201 introduced for observability/ before the
    fix landed — Grafana/Prometheus configs changed but the deploy didn't restart
    them, so changes never reached the running container.
    """
    compose = {
        "services": {
            "myapp": {
                "profiles": ["prod"],
                "volumes": ["./myapp.yaml:/etc/myapp.yaml:ro"],
            }
        }
    }
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(compose))
    (tmp_path / "deploy.yml").write_text(
        # Deploy file mentions a different config — myapp.yaml is uncovered.
        '            restart_if_changed "other.yaml" otherapp\n'
    )
    result = obs_smoke.check_cd_restart_wiring(
        tmp_path / "docker-compose.yml", tmp_path / "deploy.yml"
    )
    assert result.failures, "expected failure when restart entry is missing"
    assert any("myapp" in f and "myapp.yaml" in f for f in result.failures)


def test_check_cd_restart_wiring_prefix_match(tmp_path: Path) -> None:
    """A directory-rooted bind mount is satisfied by a restart_if_prefix_changed
    that names the service and whose prefix prefixes the mount source."""
    (tmp_path / "obs").mkdir()
    compose = {
        "services": {
            "grafana": {
                "profiles": ["prod"],
                "volumes": ["./obs:/etc/grafana:ro"],
            }
        }
    }
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(compose))
    (tmp_path / "deploy.yml").write_text('            restart_if_prefix_changed "obs/" grafana\n')
    result = obs_smoke.check_cd_restart_wiring(
        tmp_path / "docker-compose.yml", tmp_path / "deploy.yml"
    )
    assert not result.failures
    assert len(result.passes) == 1


# ─── Synthetic regression: cAdvisor without --docker_only ──────────────────


def test_obs_smoke_catches_cadvisor_missing_docker_only_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QNT-172 AC #4: prove the guard catches the PR #197 historical failure mode.

    PR #197: cAdvisor was launched without `--docker_only=true`, which made it
    enumerate every cgroup slice via /rootfs/var/lib/docker introspection. The
    introspection failed silently, producing metrics with no `name=` label.
    Every Grafana panel + alert rule that filters on `name=~"equity-data-agent-.+"`
    returned an empty vector — Containers dashboard empty, ContainerMemoryHigh
    + ContainerRestartLoop alerts permanently NoData.

    Strategy: patch the obs_smoke.py HTTP helper to simulate a Prometheus that
    answers normally for non-cAdvisor metrics but returns an empty vector for
    every query that filters on `name=~"equity-data-agent-.+"`. Then verify
    `check_grafana_panels` and `check_alert_rules_have_data` both produce
    failures naming the affected dashboards/rules.

    This is the synthetic regression test the ticket calls for: if a future
    change to obs_smoke.py loosens the contract (e.g. tolerates empty vectors)
    or the historical bug recurs, this test fails loud at CI time before any
    deploy happens.
    """

    def fake_http(url: str, timeout: float = 10.0) -> dict:
        del timeout  # signature-compatible with the real _http_get_json

        # Parse out the `query=...` parameter from /api/v1/query?query=...
        if "/api/v1/query?query=" in url:
            from urllib.parse import unquote

            expr = unquote(url.split("query=", 1)[1])
            # Simulate cAdvisor missing `name` label: any query filtering
            # on name=~"equity-data-agent-.+" returns empty.
            if 'name=~"equity-data-agent-.+"' in expr:
                return {"status": "success", "data": {"result": []}}
            # Everything else returns one fake series.
            return {
                "status": "success",
                "data": {"result": [{"metric": {}, "value": [0, "1"]}]},
            }
        raise AssertionError(f"unexpected URL in synthetic test: {url}")

    monkeypatch.setattr(obs_smoke, "_http_get_json", fake_http)

    # Panel check: every Container dashboard panel filters on the name regex,
    # so all 5 should fail. Host dashboard panels don't — they should pass.
    panel_result = obs_smoke.check_grafana_panels(
        "http://stub", REPO_ROOT / "observability" / "grafana" / "dashboards"
    )
    container_failures = [f for f in panel_result.failures if "containers-overview" in f]
    assert container_failures, (
        "obs_smoke did not flag the Containers Overview panels as broken when "
        "cAdvisor's name= label was missing — the QNT-172 regression guard is not "
        "actually guarding the PR #197 failure mode."
    )

    # Alert rule check: ContainerMemoryHigh + ContainerRestartLoop both filter
    # on the name regex, so both should fail.
    alert_result = obs_smoke.check_alert_rules_have_data(
        "http://stub",
        REPO_ROOT / "observability" / "grafana" / "provisioning" / "alerting" / "rules.yml",
    )
    failed_alerts = {
        title
        for f in alert_result.failures
        for title in ("ContainerMemoryHigh", "ContainerRestartLoop")
        if title in f
    }
    assert failed_alerts == {"ContainerMemoryHigh", "ContainerRestartLoop"}, (
        f"expected ContainerMemoryHigh + ContainerRestartLoop to fail; got {failed_alerts}"
    )


# ─── Prom timestamp parsing ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "iso,expect_zero",
    [
        ("", True),
        ("not-a-date", True),
        ("2026-05-07T10:00:00Z", False),
        ("2026-05-07T10:00:00.123Z", False),
        # Nanosecond precision — Python only does microsecond, must be trimmed.
        ("2026-05-07T10:00:00.123456789Z", False),
    ],
)
def test_parse_prom_timestamp(iso: str, expect_zero: bool) -> None:
    ts = obs_smoke._parse_prom_timestamp(iso)
    if expect_zero:
        assert ts == 0.0
    else:
        assert ts > 0
