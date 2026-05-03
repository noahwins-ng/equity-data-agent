#!/bin/bash
# QNT-103: print observability stack health.
# Run on the Hetzner host (the Prometheus + Grafana ports are loopback-bound).
# Pulls targets via the Prometheus HTTP API and pretty-prints job/health/url
# without needing jq (which isn't installed on the host).

set -uo pipefail

PROM_URL="${PROM_URL:-http://localhost:9090}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3030}"

TARGETS_FILE="$(mktemp -t obs-targets.XXXXXX)"
trap 'rm -f "$TARGETS_FILE"' EXIT

echo "=== Prometheus targets (expect 3 UP: prometheus / node_exporter / cadvisor) ==="
if ! curl -sf --max-time 5 "$PROM_URL/api/v1/targets" > "$TARGETS_FILE" 2>/dev/null; then
  echo "  prometheus unreachable at $PROM_URL"
else
  TARGETS_FILE="$TARGETS_FILE" python3 - <<'PY'
import json, os
with open(os.environ["TARGETS_FILE"]) as f:
    targets = json.load(f)["data"]["activeTargets"]
for t in targets:
    job = t["labels"]["job"]
    health = t["health"].upper()
    url = t["scrapeUrl"]
    print(f"  {job:14s} {health:6s} {url}")
PY
fi

echo ""
echo "=== Grafana health ==="
if curl -sf --max-time 5 "$GRAFANA_URL/api/health" 2>/dev/null; then
  echo ""
else
  echo "  grafana unreachable at $GRAFANA_URL"
fi

echo ""
echo "=== Host memory headroom (CX41 has 16 GiB; QNT-103 AC: >3 GiB free under load) ==="
free -h | awk 'NR==1 || NR==2'
