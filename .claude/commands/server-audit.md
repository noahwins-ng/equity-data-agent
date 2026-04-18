# Server Audit

Audit the Hetzner prod server for configuration gaps — container durability, host health, security posture, automated-update surprises, and repo drift. Classifies findings as ✓ healthy / ⚠ advisory / ✗ gap. Proposes Linear tickets for each gap found; files them **only after your approval**.

Accepts no arguments. Run whenever you want a health snapshot, or after an incident to derive follow-up work.

## Why this exists

The Apr-16 outage (CD green / prod 17 commits behind) and the Apr-18 outage (check-prod green / host rebooted / no restart policy) were both invisible until they hurt. Both had signals in logs/configs we didn't look at. This command turns "look at everything once in a while" into a repeatable ceremony that lands as tracked tickets.

## Instructions

### Step 0: Orient
1. Confirm SSH: `ssh hetzner 'echo ok'`. If that fails, stop — nothing else is reachable.
2. Read `docker-compose.yml` to know the expected prod-profile service list. Check against reality below.
3. `git rev-parse origin/main` locally — needed for the runtime-identity check.

### Step 1: Collect (run in parallel where safe)

Group findings by category. Each bullet becomes one line in the report.

**A. Container state**
- `ssh hetzner 'docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"'` — every expected service must show `Up`.
- `ssh hetzner 'docker inspect $(docker ps -qa) --format "{{.Name}}: restart={{.HostConfig.RestartPolicy.Name}} health={{if .Config.Healthcheck}}yes{{else}}no{{end}} mem={{.HostConfig.Memory}} logdriver={{.HostConfig.LogConfig.Type}}"'`
- Flag: any service with `restart=no`, `health=no`, `mem=0`, or `logdriver=json-file` without `max-size` is a durability gap.

**B. Host OS**
- `ssh hetzner 'uptime && uname -r'`
- `ssh hetzner 'df -h / /var/lib/docker'` — flag any partition >80% full.
- `ssh hetzner 'free -h && cat /proc/sys/vm/swappiness /proc/sys/vm/overcommit_memory'`
- `ssh hetzner 'ls /var/run/reboot-required 2>&1'` — if present, a reboot is pending. Read `/var/run/reboot-required.pkgs` for which package triggered it.

**C. Automated updates**
- `ssh hetzner 'grep -E "Automatic-Reboot|Mail|MailReport" /etc/apt/apt.conf.d/50unattended-upgrades | grep -v "^//"'`
- `ssh hetzner 'systemctl list-timers apt-daily-upgrade.timer --no-pager'`
- `ssh hetzner 'tail -5 /var/log/unattended-upgrades/unattended-upgrades.log 2>/dev/null'`
- Flag: `Automatic-Reboot=true` without `Mail` set = silent reboots (the exact Apr-18 setup).

**D. Security**
- `ssh hetzner 'sudo ufw status 2>/dev/null || echo "ufw not installed"'`
- `ssh hetzner 'systemctl is-active fail2ban 2>/dev/null || echo "not installed"'`
- `ssh hetzner 'grep -E "^(PermitRootLogin|PasswordAuthentication|PubkeyAuthentication)" /etc/ssh/sshd_config'`
- Flag: `PasswordAuthentication yes`, `PermitRootLogin yes`, no firewall, no fail2ban.

**E. App surface**
- `ssh hetzner 'curl -sf http://localhost:8000/api/v1/health'` — must be HTTP 200 with JSON.
- Compare `deploy.git_sha` from the JSON with local `git rev-parse origin/main` — mismatch = drift from main (either CD not yet run, or a deploy failure we didn't catch).
- Compare `deploy.dagster_assets/dagster_checks` with expected minimums (currently 8/17) — below minimum = Dagster load failure even if containers report Up.
- `make monitor-log | tail -20` — surface recent health failures.

**F. Repo drift on prod**
- `ssh hetzner 'cd /opt/equity-data-agent && git status --short'` — expected: only `health-monitor-heartbeat` and `health-monitor.log` (runtime artifacts). Anything else = SCP'd file or uncommitted change = deploy-blocking drift (see memory `feedback_prod_hotfix_scp.md`).

### Step 2: Classify each finding
- **✓ Healthy** — meets expectations. One-line mention, no ticket.
- **⚠ Advisory** — non-blocking, worth noting but not worth a ticket right now (e.g. `vm.swappiness=60`, no swap on a not-memory-pressured box).
- **✗ Gap** — known durability or security risk. Propose a Linear ticket.

### Step 3: De-dupe against existing Linear
For each ✗ Gap, search Linear for open tickets covering the same scope before proposing a new one:
```
mcp__claude_ai_Linear__list_issues with a title/description keyword search in project "Equity Data Agent".
```
If an open ticket already covers it, reference it in the report as `covered by QNT-XX` and do NOT propose a duplicate. (Remembering: `feedback_fix_pattern_not_example.md` — don't file the same gap twice under different titles.)

### Step 4: Report

```
Server Audit — <hetzner hostname> @ <timestamp UTC>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Healthy (N):
  ✓ <finding>
  ...

Advisory (N):
  ⚠ <finding> — <why it's non-blocking today>
  ...

Gaps (N, already tracked):
  ✗ <finding> — covered by QNT-XX (<title>, <status>)
  ...

Gaps (N, untracked) — proposed tickets:
  1. <title>                   [<label> / <priority>]
     <one-sentence why>
  2. ...

Summary: N healthy, N advisory, N gaps (N tracked, N new).
```

### Step 5: Confirm + file

Ask the user: "File all N proposed tickets? (y / n / pick numbers, e.g. `1,3`)"

On `y` or specific picks, for each selected ticket call `mcp__claude_ai_Linear__save_issue` with:
- `team: Quant`
- `project: Equity Data Agent`
- `labels: ["infra"]` (or appropriate — `backend` if it's app-layer)
- `priority: 2` (High) for security / outage-class gaps; `3` (Medium) for observability / nice-to-haves
- `state: Todo`
- `cycle: 1` only if user wants it on the current cycle board — otherwise leave off and it lands in Backlog per project default
- `milestone: "Ops & Reliability"` for all infra findings from this command
- `relatedTo:` any QNT-XX referenced in the gap description

After filing, list the new QNT-XXs with URLs.

### Step 6: Next step
Suggest the highest-priority new ticket as the next `/go` target. If the user wants to attend to one now, they can run `/go QNT-XX` directly.

## Caveats

- This command is **read-only on prod**. It never modifies config, restarts services, or runs destructive commands. If a gap requires a fix, that fix happens via a normal `/go QNT-XX` workflow.
- SSH access is required. If `ssh hetzner 'echo ok'` fails, stop and ask the user to fix the tunnel/key/network before retrying.
- Don't propose tickets for every advisory — reserve tickets for real durability/security gaps. Swap-not-configured on a 15GB-free host is an advisory, not a gap.
- If you can't find a matching open ticket but you're unsure whether one exists, ask the user instead of filing. A false-positive duplicate is worse than a missing ticket.
