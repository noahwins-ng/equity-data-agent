#!/bin/bash
# QNT-203: run a script (read from stdin) on the prod host over the runner's
# native OpenSSH. Replaces the third-party SSH action deploy.yml used to call,
# so the prod SSH key is never handed to code outside this repo (that action
# rode a mutable tag and downloaded its SSH binary at runtime).
#
# Usage: scripts/ci-prod-run.sh [VAR ...] <<'EOF'
#          ...script...
#        EOF
# Each named VAR is exported into the remote script with its local value
# (the replacement for ssh-action's `envs:`). Values travel on the remote
# command line (visible in prod `ps`), so never pass secrets this way.
#
# The script travels base64-encoded as a `bash -c` argument, not via
# `bash -s` on stdin, so a remote command that reads stdin can't swallow the
# rest of the script. Remote stdin is /dev/null.
#
# PROD_SSH_HOST defaults to the `prod` alias written by deploy.yml's
# "Set up SSH to prod" step (key, StrictHostKeyChecking=yes, keepalive).

set -euo pipefail

PROD_SSH_HOST="${PROD_SSH_HOST:-prod}"

script=""
for var in "$@"; do
  script+="export ${var}=$(printf '%q' "${!var}")"$'\n'
done
script+="$(cat)"

payload=$(printf '%s' "$script" | base64 | tr -d '\n')
exec ssh "$PROD_SSH_HOST" "bash -c \"\$(echo $payload | base64 -d)\"" </dev/null
