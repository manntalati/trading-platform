# infra

What runs where, today: scheduled batch jobs on a single Linux box (or VM) using **systemd user
timers**. Docker images, k3s CronJobs, Helm and Terraform come later in the roadmap; the jobs
are plain CLI commands so they move into containers unchanged.

| Unit | Schedule | Command |
|---|---|---|
| `tp-bars-daily` | Mon–Fri 18:30 America/New_York | `tp-data bars daily` |
| `tp-notify-failure@` | on failure of any unit above | journal + optional Discord webhook |

## Set up a box (Ubuntu Server)

```bash
# 1. Code + environment (uv installs Python 3.12 itself)
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/manntalati/trading-platform ~/trading-platform
cd ~/trading-platform
uv sync --locked --no-dev            # runtime packages only

# 2. Secrets: paper keys only. .env is git-ignored; keep it readable by you alone.
cp .env.example .env && chmod 600 .env && $EDITOR .env

# 3. Smoke test, then the one-off 5-year backfill
.venv/bin/tp-data check
.venv/bin/tp-data bars backfill --years 5

# 4. Clock sync matters for timestamps (and later, for latency measurements)
timedatectl status                  # expect "System clock synchronized: yes"

# 5. Install the timers as user units and keep them running when logged out
mkdir -p ~/.config/systemd/user
cp infra/systemd/*.service infra/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tp-bars-daily.timer
sudo loginctl enable-linger "$USER"
```

The units assume the repo lives at `~/trading-platform`; edit `WorkingDirectory`/`ExecStart`
if not.

## Operate

```bash
systemctl --user list-timers 'tp-*'                 # next/last run
systemctl --user start tp-bars-daily.service        # run now
journalctl --user -u tp-bars-daily -n 100           # logs of recent runs
systemctl --user status tp-bars-daily.service       # last exit code
```

Exit codes: `0` ok (warnings allowed), `1` validation errors (rows quarantined; see
[docs/data.md](../docs/data.md#when-the-daily-job-fails)), `2` configuration problem.

## Without systemd

[`cron/crontab.example`](cron/crontab.example) does the same with cron (UTC times; no failure
hook).
