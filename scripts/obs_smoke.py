"""Pre-prod assertion that the observability stack is actually wired up (QNT-172).

Phase 7 retro found that QNT-103 (Prometheus + Grafana + cAdvisor + node_exporter)
shipped clean against unit tests but landed 5 follow-up PRs in prod within 24h —
all silent NoData / empty-panel / never-fires failures that unit tests cannot
catch by design.

This script asserts four invariants the unit suite cannot:

    1. Every Prometheus scrape target is healthy and recently scraped.
    2. Every Grafana dashboard panel query returns at least one series.
    3. Every Grafana alert rule's underlying data exists (so the rule is not
       permanently NoData / Unknown).
    4. Every bind-mounted config file in docker-compose.yml has a matching
       `restart_if_(prefix_)changed` entry in .github/workflows/deploy.yml,
       so a config edit actually reaches the running consumer.

Usage:
    python scripts/obs_smoke.py                     # localhost URLs (via tunnel)
    PROM_URL=http://prom:9090 python scripts/obs_smoke.py
    python scripts/obs_smoke.py --skip-runtime      # config wiring (4) only;
                                                    # for CI without a running stack

Exit code 0 on full pass, 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARDS_DIR = REPO_ROOT / "observability" / "grafana" / "dashboards"
ALERT_RULES_FILE = (
    REPO_ROOT / "observability" / "grafana" / "provisioning" / "alerting" / "rules.yml"
)
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

DEFAULT_PROM_URL = os.environ.get("PROM_URL", "http://localhost:9090")
DEFAULT_GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3030")

# Profiles whose services are exercised by `docker compose --profile prod up`.
# Defensive filter: skips services whose `profiles:` is set and disjoint from
# this set. Currently a no-op (every compose service is in `prod` or `dev,prod`),
# but reserved for future dormant profiles.
ACTIVE_PROFILES: set[str] = {"prod"}


@dataclass
class Result:
    failures: list[str] = field(default_factory=list)
    passes: list[str] = field(default_factory=list)

    def add_pass(self, msg: str) -> None:
        self.passes.append(msg)
        print(f"  PASS  {msg}")

    def add_fail(self, msg: str) -> None:
        self.failures.append(msg)
        print(f"  FAIL  {msg}")

    def merge(self, other: Result) -> None:
        self.failures.extend(other.failures)
        self.passes.extend(other.passes)


def _http_get_json(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - trusted localhost
        return json.loads(r.read().decode("utf-8"))


# ────────────────────────────────────────────────────────────────────────────
# Check 1: Prometheus targets are UP and recently scraped.
# ────────────────────────────────────────────────────────────────────────────


def check_prometheus_targets(prom_url: str, max_stale_s: float = 120.0) -> Result:
    """Every active scrape target must be UP with lastScrape within `max_stale_s`.

    The Prometheus default scrape interval is 30s; we allow 120s so transient
    scrape blips don't false-fail. The original ticket spec said 60s but real
    deploys can cause one or two missed scrapes during recreate.
    """
    print(f"\n[1/4] Prometheus targets at {prom_url}")
    out = Result()

    try:
        data = _http_get_json(f"{prom_url}/api/v1/targets")
    except urllib.error.URLError as e:
        out.add_fail(f"prometheus unreachable: {e}")
        return out

    targets = data.get("data", {}).get("activeTargets", [])
    if not targets:
        out.add_fail("prometheus has zero active scrape targets — config not loaded")
        return out

    now = time.time()
    for t in targets:
        job = t["labels"].get("job", "?")
        health = t.get("health", "unknown")
        last_scrape_iso = t.get("lastScrape", "")
        # ISO 8601 with sub-second precision: "2026-05-07T10:00:00.123456789Z".
        # Truncate to microseconds + replace Z so fromisoformat accepts it.
        last_scrape_s = _parse_prom_timestamp(last_scrape_iso)
        age = now - last_scrape_s if last_scrape_s else float("inf")
        last_err = t.get("lastError", "")

        label = f"target {job} ({t['labels'].get('instance', '?')})"
        if health != "up":
            out.add_fail(f"{label} health={health}, lastError={last_err!r}")
        elif age > max_stale_s:
            out.add_fail(f"{label} lastScrape {age:.0f}s ago > {max_stale_s:.0f}s")
        else:
            out.add_pass(f"{label} up, lastScrape {age:.0f}s ago")
    return out


def _parse_prom_timestamp(iso: str) -> float:
    # "2026-05-07T10:00:00.123456789Z" -> drop trailing Z, trim sub-microsecond
    # digits Python can't parse.
    if not iso:
        return 0.0
    s = iso.rstrip("Z")
    # split on '.' to clip nanoseconds → microseconds
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    try:
        from datetime import datetime

        return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return 0.0


# ────────────────────────────────────────────────────────────────────────────
# Check 2: every Grafana dashboard panel query returns at least one series.
# ────────────────────────────────────────────────────────────────────────────


def _walk_panels(panels: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    """Yield every panel, descending into row containers."""
    for p in panels:
        yield p
        if "panels" in p and isinstance(p["panels"], list):
            yield from _walk_panels(p["panels"])


def extract_panel_queries(dashboards_dir: Path) -> list[tuple[str, str, str]]:
    """Return [(dashboard_uid, panel_title, expr)] for every Prometheus panel target."""
    out: list[tuple[str, str, str]] = []
    for f in sorted(dashboards_dir.glob("*.json")):
        with f.open() as fh:
            dash = json.load(fh)
        uid = dash.get("uid", f.stem)
        for panel in _walk_panels(dash.get("panels", [])):
            ds = panel.get("datasource") or {}
            # Skip rows / text panels — they have no targets.
            if panel.get("type") in {"row", "text"}:
                continue
            for tgt in panel.get("targets", []) or []:
                expr = (tgt.get("expr") or "").strip()
                if not expr:
                    continue
                # Only run Prometheus-typed targets (current dashboards: 100%
                # prometheus). Datasource type can live on the panel or target.
                tgt_ds = tgt.get("datasource") or ds
                tgt_type = tgt_ds.get("type") if isinstance(tgt_ds, dict) else None
                if tgt_type and tgt_type != "prometheus":
                    continue
                out.append((uid, panel.get("title", "(untitled)"), expr))
    return out


def check_grafana_panels(prom_url: str, dashboards_dir: Path) -> Result:
    print(f"\n[2/4] Grafana dashboard panel queries (via {prom_url})")
    out = Result()
    queries = extract_panel_queries(dashboards_dir)
    if not queries:
        out.add_fail(f"no panels with Prometheus queries found under {dashboards_dir}")
        return out

    for uid, title, expr in queries:
        ok, detail = _prom_query_returns_data(prom_url, expr)
        label = f"{uid} → {title!r}"
        if ok:
            out.add_pass(f"{label}: {detail}")
        else:
            out.add_fail(f"{label}: {detail} | expr={_truncate(expr, 120)}")
    return out


def _prom_query_returns_data(prom_url: str, expr: str) -> tuple[bool, str]:
    """Run an instant Prometheus query; return (non_empty, summary)."""
    try:
        url = f"{prom_url}/api/v1/query?query={urllib.parse.quote(expr)}"
        data = _http_get_json(url)
    except urllib.error.URLError as e:
        return False, f"query error: {e}"
    if data.get("status") != "success":
        return False, f"non-success status: {data.get('errorType')}: {data.get('error')}"
    result = data.get("data", {}).get("result", [])
    if not result:
        return False, "empty result vector (NoData)"
    return True, f"{len(result)} series"


def _truncate(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ────────────────────────────────────────────────────────────────────────────
# Check 3: every Grafana alert rule's underlying data exists.
# ────────────────────────────────────────────────────────────────────────────


def extract_alert_exprs(rules_file: Path) -> list[tuple[str, str]]:
    """Return [(rule_title, query_expr)] for each alert rule.

    The rule body uses Grafana's multi-step model with refIds; the actual
    PromQL lives on the data step whose `datasourceUid: prometheus`. (The
    reduce/threshold steps run on the math expression engine, not Prometheus.)
    """
    with rules_file.open() as f:
        cfg = yaml.safe_load(f)
    out: list[tuple[str, str]] = []
    for group in cfg.get("groups", []):
        for rule in group.get("rules", []):
            title = rule.get("title", rule.get("uid", "?"))
            for step in rule.get("data", []):
                if step.get("datasourceUid") != "prometheus":
                    continue
                expr = (step.get("model", {}).get("expr") or "").strip()
                if expr:
                    out.append((title, expr))
                    break  # one expr per rule is enough — the reduce step folds A
    return out


def check_alert_rules_have_data(prom_url: str, rules_file: Path) -> Result:
    print(f"\n[3/4] Grafana alert rule data sources (via {prom_url})")
    out = Result()
    exprs = extract_alert_exprs(rules_file)
    if not exprs:
        out.add_fail(f"no alert rules with Prometheus exprs found under {rules_file}")
        return out

    for title, expr in exprs:
        ok, detail = _prom_query_returns_data(prom_url, expr)
        if ok:
            out.add_pass(f"alert {title!r}: {detail}")
        else:
            out.add_fail(f"alert {title!r}: {detail} | expr={_truncate(expr, 120)}")
    return out


# ────────────────────────────────────────────────────────────────────────────
# Check 4: every bind-mounted config in compose has a deploy.yml restart entry.
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BindMount:
    service: str
    source: str  # repo-relative, e.g. "litellm_config.yaml" or "observability/grafana/"
    is_dir: bool


def extract_compose_bind_mounts(compose_file: Path) -> list[BindMount]:
    """Repo-relative bind mounts for services whose profile is in ACTIVE_PROFILES.

    Skips:
      * named volumes (e.g. `dagster_home:/dagster_home`)
      * absolute-path mounts (`/var/run/...`, `/:/rootfs`) — these are runtime
        host paths, not config files maintained in git.
      * services whose `profiles:` field is set and disjoint from ACTIVE_PROFILES.
    """
    with compose_file.open() as f:
        compose = yaml.safe_load(f)

    mounts: list[BindMount] = []
    for svc_name, svc in (compose.get("services") or {}).items():
        profiles = set(svc.get("profiles") or [])
        # Service with no `profiles:` runs in every invocation; otherwise it
        # must intersect ACTIVE_PROFILES to be considered "running in prod".
        if profiles and not (profiles & ACTIVE_PROFILES):
            continue
        for vol in svc.get("volumes") or []:
            if not isinstance(vol, str):
                continue
            # "src:dst[:mode]" — source is everything before the first colon.
            src = vol.split(":", 1)[0]
            if not src.startswith("./"):
                continue
            rel = src[2:]  # strip leading "./"
            # Heuristic: trailing-slash or no extension → treat as directory.
            # Compose source paths in this repo follow this convention; the
            # existing entries are litellm_config.yaml, dagster.yaml,
            # workspace.yaml, prometheus.yml, grafana/provisioning,
            # grafana/dashboards.
            on_disk = compose_file.parent / rel
            is_dir = on_disk.is_dir()
            mounts.append(BindMount(service=svc_name, source=rel, is_dir=is_dir))
    return mounts


def parse_deploy_restart_directives(deploy_file: Path) -> list[tuple[str, str, list[str]]]:
    """Return [(kind, source, services)] from the deploy.yml restart step.

    `kind` is "exact" for restart_if_changed, "prefix" for restart_if_prefix_changed.
    """
    text = deploy_file.read_text()
    out: list[tuple[str, str, list[str]]] = []
    # Each line looks like:
    #   restart_if_changed "litellm_config.yaml" litellm
    #   restart_if_changed "dagster.yaml" dagster dagster-code-server dagster-daemon
    #   restart_if_prefix_changed "observability/grafana/" grafana
    pattern = re.compile(
        r'^\s*restart_if_(changed|prefix_changed)\s+"([^"]+)"\s+(.+?)\s*$',
        re.MULTILINE,
    )
    for m in pattern.finditer(text):
        kind = "exact" if m.group(1) == "changed" else "prefix"
        source = m.group(2)
        services = m.group(3).split()
        out.append((kind, source, services))
    return out


def check_cd_restart_wiring(compose_file: Path, deploy_file: Path) -> Result:
    """Every (service, repo-relative source) bind mount must be covered by a
    restart_if_changed (exact match) or restart_if_prefix_changed (source starts
    with the given prefix) entry that names the consuming service.
    """
    print(f"\n[4/4] CD config-mount → restart wiring ({deploy_file.name})")
    out = Result()
    mounts = extract_compose_bind_mounts(compose_file)
    directives = parse_deploy_restart_directives(deploy_file)
    if not directives:
        out.add_fail(
            f"no restart_if_(prefix_)changed directives found in {deploy_file} — "
            "did the step rename or move?"
        )
        return out

    for mount in mounts:
        if _mount_covered(mount, directives):
            out.add_pass(f"{mount.service} mount {mount.source} covered by deploy.yml")
        else:
            out.add_fail(
                f"{mount.service} bind-mounts {mount.source!r} but deploy.yml has no "
                f"restart_if_(prefix_)changed entry restarting {mount.service} on changes"
            )
    return out


def _mount_covered(mount: BindMount, directives: list[tuple[str, str, list[str]]]) -> bool:
    for kind, source, services in directives:
        if mount.service not in services:
            continue
        if kind == "exact" and source == mount.source:
            return True
        if kind == "prefix" and (
            mount.source == source.rstrip("/")
            or mount.source.startswith(source if source.endswith("/") else source + "/")
        ):
            return True
    return False


# ────────────────────────────────────────────────────────────────────────────
# Driver
# ────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prom-url", default=DEFAULT_PROM_URL)
    parser.add_argument("--grafana-url", default=DEFAULT_GRAFANA_URL)
    parser.add_argument(
        "--skip-runtime",
        action="store_true",
        help="Skip checks 1-3 (Prom/Grafana queries). Use in CI without a live stack.",
    )
    parser.add_argument(
        "--only",
        choices=["targets", "panels", "alerts", "wiring"],
        help="Run only one of the four checks.",
    )
    args = parser.parse_args(argv)

    print("obs-smoke: QNT-172 pre-prod observability assertion")
    print(f"  PROM_URL={args.prom_url}")
    print(f"  GRAFANA_URL={args.grafana_url}")

    overall = Result()
    selected = {args.only} if args.only else {"targets", "panels", "alerts", "wiring"}

    if not args.skip_runtime and "targets" in selected:
        overall.merge(check_prometheus_targets(args.prom_url))
    if not args.skip_runtime and "panels" in selected:
        overall.merge(check_grafana_panels(args.prom_url, DASHBOARDS_DIR))
    if not args.skip_runtime and "alerts" in selected:
        overall.merge(check_alert_rules_have_data(args.prom_url, ALERT_RULES_FILE))
    if "wiring" in selected:
        overall.merge(check_cd_restart_wiring(COMPOSE_FILE, DEPLOY_WORKFLOW))

    print()
    print(f"obs-smoke: {len(overall.passes)} passed, {len(overall.failures)} failed")
    if overall.failures:
        print("\nFailures:")
        for f in overall.failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
