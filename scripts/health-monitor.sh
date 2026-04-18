#!/bin/bash
# Health monitor for prod — runs as a cron job on Hetzner
# Checks API health and docker service status, logs failures
# Install: make monitor-install
# Check:   make monitor-log

LOG="/opt/equity-data-agent/health-monitor.log"
MAX_LINES=500

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

# Check API health
if curl -sf --max-time 10 http://localhost:8000/health > /dev/null 2>&1; then
  API_STATUS="ok"
else
  API_STATUS="FAIL"
  echo "$(timestamp) API health check FAILED — /health unreachable" >> "$LOG"
fi

# Check docker services are running
SERVICES=$(cd /opt/equity-data-agent && docker compose --profile prod ps --format '{{.Name}} {{.Status}}' 2>/dev/null)
DOWN_SERVICES=$(echo "$SERVICES" | grep -v "Up" | grep -v "^$")

if [ -n "$DOWN_SERVICES" ]; then
  echo "$(timestamp) SERVICES DOWN: $DOWN_SERVICES" >> "$LOG"
fi

# Surface pending kernel reboots. unattended-upgrades touches
# /var/run/reboot-required when a package (usually kernel or libc) needs a
# host reboot. Left unattended, the next host reboot can silently drop prod
# — see 2026-04-18 incident and QNT-95.
if [ -f /var/run/reboot-required ]; then
  if [ -f /var/run/reboot-required.pkgs ]; then
    PKGS=$(tr '\n' ',' < /var/run/reboot-required.pkgs | sed 's/,$//')
  else
    PKGS=""
  fi
  echo "$(timestamp) REBOOT REQUIRED: ${PKGS:-unknown}" >> "$LOG"
fi

# If everything is fine, write a heartbeat (one line, overwritten each run)
if [ "$API_STATUS" = "ok" ] && [ -z "$DOWN_SERVICES" ]; then
  echo "$(timestamp) OK" > /opt/equity-data-agent/health-monitor-heartbeat
fi

# Trim log to prevent unbounded growth
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt "$MAX_LINES" ]; then
  tail -n "$MAX_LINES" "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
