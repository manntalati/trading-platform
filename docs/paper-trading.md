# Paper trading

Library strategies trade **Alpaca's paper account**, each in its own capital sleeve, through the
same code that backtests them ([backtesting.md](backtesting.md)) and the same risk checks
([risk.md](risk.md)). This is stage 2 of the plan's promotion path: a strategy earns a look at
small real money only after at least 60 trading days and 30 trades on paper, with results
consistent with its backtest.

There is no live mode. The broker client is always created with `paper=True`, and your Fidelity
account is read-only (portfolio tracking only); nothing here can trade it.

## The bot

`tp-paper bot` runs the whole cycle by itself, every trading day, with nothing to approve:

```
09:10 ET  submit       queued proposals -> market-on-open orders (window closes 09:28)
09:30 ET  open         orders fill in the opening auction
09:45 ET  sync_open    fills recorded, sleeves updated, positions reconciled with the broker
16:30 ET  sync_close   order updates after the close (30 minutes after an early close)
18:45 ET  propose      fetch today's bars if the lake is behind, mark every sleeve at the close,
                       run every strategy, risk-check every order, queue what passes
```

The times come from the exchange calendar, so holidays and half days take care of themselves.
Signals use the session's close and fills happen at the next open, exactly as the backtest's
default `next_open` fill model assumes.

It is built to be left alone:

- **Restarts are safe.** Each task is recorded per session once it's done, so a restart never
  repeats an order; a task whose window closed while the bot was down is logged as missed, and
  proposals that were never sent expire at the next evening's run.
- **Failures retry.** A task that fails (broker or data feed down) is retried every 15 minutes
  for about two hours, then logged as failed; the next session starts clean.
- **It reports in.** A heartbeat every few minutes, the next task, and the last result show on
  the dashboard's Paper tab and in `tp-paper status`; everything it does goes to the activity
  log.
- **The guard rails stay on.** Every order passes the risk checks; the kill switch stops it
  submitting; a sleeve that breaks its drawdown limit is disabled until you run
  `tp-risk enable <name>` (the one thing that needs you).

Run it as a service (`infra/systemd/tp-paper-bot.service`, see
[infra/README.md](../infra/README.md)), in a terminal (`uv run tp-paper bot`), or from cron
with `tp-paper bot --once` every five minutes.

## Set up

1. Use a **dedicated Alpaca paper account** (Alpaca lets you add paper accounts) and put its
   paper keys in `.env` (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`). Positions you open by hand in
   the same account show up as reconciliation breaks.
2. Choose the book in [`config/paper.toml`](../config/paper.toml): which strategies, how much
   capital each (the sleeves must fit inside the account's equity, $100k by default), approval
   mode and parameters. All five library strategies start with $20k each on automatic approval.
3. Backfill the bars once (`uv run tp-data bars backfill --years 5`), then start the bot:

```bash
uv run tp-paper bot                       # or: systemctl --user enable --now tp-paper-bot
uv run tp-paper status                    # sleeves, gate progress, bot heartbeat
```

No keys yet? `TP_PAPER_BROKER=fake uv run tp-paper bot --bars fake` simulates the account and
the data feed, so the whole thing runs offline.

The bot reads `config/paper.toml` and `config/risk.toml` when it starts: restart it after
editing them (`systemctl --user restart tp-paper-bot`). The kill switch and `tp-risk
enable`/`disable` take effect immediately.

The individual steps are still there for running a cycle by hand or debugging one:
`tp-paper propose`, `submit`, `sync`, `proposals`, `approve`, `reject`, `kill`.

## Approval

- `approval = "auto"` (default): proposals that pass the risk checks are approved on the spot
  and go out at the next open. You can still see them on the dashboard ("Queued for the next
  open", with the strategy's reason and the risk checks) and stop any of them with **Don't
  trade** (or `tp-paper reject <id>`) before 09:10.
- `approval = "manual"`: a strategy's proposals wait for you instead. You can approve fewer
  shares than proposed, never more (more would skip the risk checks); anything undecided at
  09:10 expires.

Unattended trading is for paper only. The plan keeps a person on every order once real money is
involved (stage 3), and nothing in this package can reach a live account.

Every decision is stored with who made it (`auto`, `you`, `dashboard`) and when, so you can later
measure whether your vetoes helped or hurt.

## Sleeves, reconciliation and the gate

Each strategy's positions and cash are rebuilt from its own fills, so two strategies can hold
the same ETF and each knows its share. After every sync the sum of all sleeves is compared with
the broker's positions; any difference is a **reconciliation break**, logged and shown in
`tp-paper status` (the plan's gate requires zero).

`tp-paper status` shows, per sleeve: equity, return and drawdown since it started, trading days
and trades against the gate's 60 and 30, Sharpe once there are 20 days, and **slippage**: the
average fill price against the session's official open, in basis points, next to the 5 bps the
backtest assumes.

Risk applies at two moments: when proposals are made (all checks, against the whole account,
with earlier proposals counted), and at submission (kill switch; disabled strategies can't buy).
A sleeve whose drawdown breaks its limit is disabled until you run `tp-risk enable <name>`.

## Stop everything

```bash
uv run tp-paper kill --reason "what happened"    # cancel open orders, block new ones
uv run tp-risk resume                            # when you're ready again
```

## State

| Where | What |
|---|---|
| `data/state/paper.sqlite` | proposals and their lifecycle, fills, daily sleeve equity, event log, latest reconciliation |
| `data/state/risk.json` | kill switch, disabled strategies, peak equity per sleeve |
| `data/state/fake_broker.json` | the simulated account (only with `--broker fake`) |

Proposal lifecycle: `pending → approved → submitted → filled`, with `rejected` (by you),
`blocked` (by risk), `expired` (undecided or stale), `canceled` (unfilled at the open, or the
kill switch) and `failed` (the broker refused it) on the way out.

## Not modelled yet

- Fees: Alpaca paper charges none; the backtest models them, so paper P&L is slightly flattering.
- Paper fills are Alpaca's simulation, not real executions: treat the slippage column as an
  estimate until a strategy trades real (micro) money.
- Corporate actions between proposal and fill (a split on the fill day changes share counts).
- Options strategies (rejected by risk until the options module exists).
