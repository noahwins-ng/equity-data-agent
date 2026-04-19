#!/bin/bash
# Docker events → Discord webhook notifier (QNT-101).
#
# Streams `docker events` for die/kill/oom/restart on compose-managed containers
# and posts a formatted alert to a Discord webhook. Writes a heartbeat file
# (and optionally pings an external heartbeat URL) every 60s so the monitor
# itself is monitorable.
#
# Required env:
#   DISCORD_WEBHOOK_URL   Discord webhook (Server Settings → Integrations)
# Optional env:
#   HEARTBEAT_URL         External heartbeat URL pinged every 60s (e.g.
#                         BetterStack heartbeat monitor)
#   COMPOSE_PROJECT       Container-name prefix filter (default: equity-data-agent)
#   HEARTBEAT_FILE        Local heartbeat path (default: /opt/equity-data-agent/events-notify-heartbeat)
#
# Install: make events-notify-install

set -uo pipefail

if [ -z "${DISCORD_WEBHOOK_URL:-}" ]; then
  echo "DISCORD_WEBHOOK_URL is required" >&2
  exit 1
fi

HEARTBEAT_URL="${HEARTBEAT_URL:-}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-equity-data-agent}"
HEARTBEAT_FILE="${HEARTBEAT_FILE:-/opt/equity-data-agent/events-notify-heartbeat}"
HOST="$(hostname)"

# --- helpers ---------------------------------------------------------------

# Escape a string for safe embedding in a JSON string literal.
# Pure bash (no jq dependency) — keeps the host install footprint tiny.
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

notify() {
  local content="$1"
  # Discord hard-limits content to 2000 chars; truncate to leave headroom.
  if [ "${#content}" -gt 1900 ]; then
    content="${content:0:1900}..."
  fi
  local body
  body="{\"content\":\"$(json_escape "$content")\"}"
  curl -sS -X POST "$DISCORD_WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    --max-time 10 \
    -d "$body" >/dev/null 2>&1 || true
}

heartbeat() {
  date -u '+%Y-%m-%dT%H:%M:%SZ' > "$HEARTBEAT_FILE" 2>/dev/null || true
  if [ -n "$HEARTBEAT_URL" ]; then
    curl -sS --max-time 5 "$HEARTBEAT_URL" >/dev/null 2>&1 || true
  fi
}

# --- startup ---------------------------------------------------------------

heartbeat
notify "[START] docker-events-notify on \`${HOST}\`"

# Background heartbeat loop — tick every 60s.
(
  while true; do
    sleep 60
    heartbeat
  done
) &
HEARTBEAT_PID=$!
# shellcheck disable=SC2064
trap "kill ${HEARTBEAT_PID} 2>/dev/null || true" EXIT INT TERM

# --- event loop ------------------------------------------------------------

# Subscribe to container lifecycle events. The daemon-side --filter flags are
# applied in parallel (OR logic across event values), so we get die+kill+oom+restart.
# Container-name filtering is done client-side because --filter name= accepts
# exact matches only — not prefixes.
docker events \
  --filter 'type=container' \
  --filter 'event=die' \
  --filter 'event=kill' \
  --filter 'event=oom' \
  --filter 'event=restart' \
  --format '{{.Action}}|{{.Actor.Attributes.name}}|{{.Actor.Attributes.exitCode}}|{{.Actor.Attributes.image}}' \
  2>&1 | while IFS='|' read -r action name exit_code image; do

  # Filter to our compose project by container-name prefix.
  case "$name" in
    "${COMPOSE_PROJECT}"-*) ;;
    *) continue ;;
  esac

  # Label per action — plain text, no emoji (grep-friendly in logs too).
  case "$action" in
    oom)     label="[OOM KILL]" ;;
    die)     label="[DIE]" ;;
    kill)    label="[KILL]" ;;
    restart) label="[RESTART]" ;;
    *)       label="[${action^^}]" ;;
  esac

  # Grab last log lines for context. Trim to ~1200 chars so the whole message
  # fits inside Discord's 2000-char content limit after JSON escaping.
  logs="$(docker logs "$name" --tail 15 2>&1 | tail -c 1200 || true)"

  msg="${label} \`${name}\` exit=${exit_code:-n/a} image=\`${image}\` host=\`${HOST}\`"$'\n'"\`\`\`"$'\n'"${logs}"$'\n'"\`\`\`"

  notify "$msg"
done
