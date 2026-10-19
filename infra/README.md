# infra

What runs where, today: scheduled batch jobs on a single Linux box (or VM) using **systemd user
timers**. Docker images, k3s CronJobs, Helm and Terraform come later in the roadmap; the jobs
are plain CLI commands so they move into containers unchanged.

| Unit | Schedule | Command |
|---|---|---|
| `tp-bars-daily` | Mon–Fri 18:30 America/New_York | `tp-data bars daily` |
| `tp-options-snapshot` | Mon–Fri 15:45 America/New_York | `tp-data options snapshot` |
| `tp-macro-daily` | Mon–Fri 07:30 America/New_York | `tp-data macro daily`: VIX and Treasury yields (Cboe, the Treasury, or FRED) |
| `tp-fundamentals` | Mon–Fri 20:00 America/New_York | `tp-data fundamentals`: company facts and earnings dates from SEC EDGAR (needs `TP_SEC_USER_AGENT`) |
| `tp-broker-sync` | Mon–Fri hourly 07:30–21:30 America/New_York | `tp-broker sync --source snaptrade` |
| `tp-paper-bot` | always on (service, restarts on failure) | `tp-paper bot`: the unattended paper-trading cycle |
| `tp-api` | always on (service, restarts on failure) | `tp-api --host 127.0.0.1 --quotes alpaca`: the dashboard, and the job scheduler that runs every job above (and the recommendations) itself, catching up on missed windows |
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
systemctl --user enable --now tp-bars-daily.timer tp-options-snapshot.timer tp-broker-sync.timer \
  tp-macro-daily.timer tp-fundamentals.timer
systemctl --user enable --now tp-paper-bot.service
systemctl --user enable --now tp-api.service
sudo loginctl enable-linger "$USER"
```

The units assume the repo lives at `~/trading-platform`; edit `WorkingDirectory`/`ExecStart`
if not.

`uv run tp-doctor` confirms each timer is enabled and each dataset is fresh.

## Operate

```bash
systemctl --user list-timers 'tp-*'                 # next/last run
systemctl --user start tp-bars-daily.service        # run now
journalctl --user -u tp-bars-daily -n 100           # logs of recent runs
systemctl --user status tp-bars-daily.service       # last exit code
```

Exit codes: `0` ok (warnings allowed), `1` validation errors (rows quarantined; see
[docs/data.md](../docs/data.md#when-the-daily-job-fails)) or, for the options snapshot, at least
one underlying failed; `2` configuration problem.

The options timer deliberately has `Persistent=false`: a snapshot taken hours late (e.g. at boot
the next morning) would be stamped with the wrong day's market, so a missed day stays missed.

## The built-in scheduler

`tp-api` (and `tp-jobs run`, without the dashboard) runs each job above in its window, catches
up on a window missed while the machine was off or asleep, skips a job whose data is already
fresh, and makes new recommendations (`tp-recs run`) whenever a job brought new data. With the
`tp-api` service running, the timers are optional: kept, they run first and the scheduler only
fills in what they missed. `uv run tp-jobs status` shows each job; logs in `data/logs/`.
Details: [docs/setup.md](../docs/setup.md#6-schedules).

## macOS

```bash
make mac-agent          # launchd agent: tp-api from login, restarted if it stops
make mac-agent-remove   # undo
```

The agent ([`launchd/com.trading-platform.api.plist`](launchd/com.trading-platform.api.plist))
runs `.venv/bin/tp-api --quotes alpaca` in the repo, logging to `data/logs/tp-api.log`. Stop any
`tp-api` you started in a terminal first (they'd share port 8000).

## Without systemd

[`cron/crontab.example`](cron/crontab.example) does the same with cron (UTC times; no failure
hook), or run `tp-jobs once` from cron every five minutes.
