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
09:10 ET  submit       queued proposals -> market-on-open orders (window closes 09:28;
                       later in the day they go out as plain market orders instead)
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
  for about two hours, then logged as failed; the next session starts clean. With no network at
  all (a laptop that just woke up and is still reconnecting) it tries again every 2 minutes,
  until the task's window closes.
- **It reports in.** A heartbeat every few minutes, the next task, and the last result show on
  the dashboard's Paper tab and in `tp-paper status`; everything it does goes to the activity
  log. Its P&L and latest fills are on the Overview, and every fill is on the Trades tab next to
  your own trades.
- **The guard rails stay on.** Every order passes the risk checks; the kill switch stops it
  submitting; a sleeve that breaks its drawdown limit is disabled until you run
  `tp-risk enable <name>` (the one thing that needs you).

Run it as a service (`infra/systemd/tp-paper-bot.service`, see
[infra/README.md](../infra/README.md)), in a terminal (`uv run tp-paper bot`), or from cron
with `tp-paper bot --once` every five minutes.

### When will I see trades?

- **Right after you start it, if it's a trading day.** The bot picks up from the most recent
  close: it proposes from it if that hasn't been done (until 30 minutes before the next close),
  and if the opening auction has already passed it sends the orders as plain market orders
  (until 15 minutes before the close), then syncs a couple of minutes later to record the
  fills. Started after 15:45 ET, or on a weekend, the orders go to the next opening auction.
- **New sleeves take their positions at once.** MA timing, both momentum strategies and dual
  momentum rebalance on the last session of the month. A sleeve that has never traded doesn't
  wait for that: on its first run it takes the positions its latest signal calls for (MA timing
  uses the last month-end), then follows the monthly schedule.
- **Most days after that are quiet, except for one sleeve.** The monthly strategies trade once
  a month, and RSI(2) only when one of its names gets oversold (it exits on strength), so a run
  that proposes nothing for them is normal. The leveraged-momentum sleeve is the exception: it
  rebalances every evening and trades on most days. The dashboard's activity log and `tp-paper status` show what every run
  decided, including "no trades proposed" and anything the risk checks blocked.

## Set up

1. Use a **dedicated Alpaca paper account** (Alpaca lets you add paper accounts) and put its
   paper keys in `.env` (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`). Positions you open by hand in
   the same account show up as reconciliation breaks.
2. Choose the book in [`config/paper.toml`](../config/paper.toml): which strategies, how much
   capital each (the sleeves must fit inside the account's equity, $100k by default), approval
   mode and parameters. Five library strategies have $18k each, and the high-risk daily
   sleeve (strategy 6, 3x leveraged funds) has $10k. All are on automatic approval.
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

### Adding a strategy or moving capital

Edit `config/paper.toml` and restart the bot. The sleeves must still fit inside the account's
equity.

- **A new strategy's symbols** get their history fetched the first evening (the bot's bars
  refresh backfills symbols the lake doesn't have). The ingest job's universe
  (`config/universes.toml`) should list them too, so the evening bars job keeps them current.
  Until the bars are there, the sleeve is skipped and the others trade as usual.
- **Changing a sleeve's capital** is treated as money moved in or out, not as a gain or a loss.
  Its returns, Sharpe and the dashboard's growth curve leave the move out, and its drawdown
  peak moves in proportion. The next evening the strategy rebalances to the new amount instead
  of waiting for its next scheduled rebalance. The activity log records the move.

Each strategy's positions and cash are rebuilt from its own fills, so two strategies can hold
the same ETF and each knows its share. After every sync the sum of all sleeves is compared with
the broker's positions; any difference is a **reconciliation break**, logged and shown in
`tp-paper status` (the plan's gate requires zero).

`tp-paper status` shows, per sleeve: equity, return and drawdown since it started, trading days
and trades against the gate's 60 and 30, Sharpe once there are 20 days, and **slippage**: the
average fill price against the session's official open, in basis points, next to the 5 bps the
backtest assumes. Only opening-auction fills count; market orders sent later in the day (after a
late start) fill at intraday prices that the open says nothing about.

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
