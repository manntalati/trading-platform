# Paper trading

Library strategies trade **Alpaca's paper account**, each in its own capital sleeve, through the
same code that backtests them ([backtesting.md](backtesting.md)) and the same risk checks
([risk.md](risk.md)). This is stage 2 of the plan's promotion path: a strategy earns a look at
small real money only after at least 60 trading days and 30 trades on paper, with results
consistent with its backtest.

There is no live mode. The broker client is always created with `paper=True`, and your Fidelity
account is read-only (portfolio tracking only); nothing here can trade it.

## The daily cycle

```
18:30 ET  tp-data bars daily          today's bars into the lake
18:50 ET  tp-paper propose            sync fills, mark sleeves at the close, run strategies,
                                      risk-check every intent, store proposals
evening   you approve / reject        dashboard, or tp-paper approve / reject
09:10 ET  tp-paper submit             approved -> market-on-open orders; undecided -> expired
09:30 ET  opening auction             orders fill at the official open
09:45 ET  tp-paper sync               fills recorded, positions reconciled with the broker
16:30 ET  tp-paper sync               again after the close
```

The systemd timers in `infra/systemd` run the four `tp-paper` steps on that schedule
([infra/README.md](../infra/README.md)). Signals use the session's close; fills happen at the
next open, exactly as the backtest's default `next_open` fill model assumes.

## Set up

1. Use a **dedicated Alpaca paper account** (Alpaca lets you add paper accounts) and
   put its paper keys in `.env` (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`). Positions you open by
   hand in the same account show up as reconciliation breaks.
2. Choose the book in [`config/paper.toml`](../config/paper.toml): which strategies, how much
   capital each (the sleeves must fit inside the account's equity, $100k by default), manual or
   automatic approval, and parameters. All five library strategies start enabled with $20k each
   and manual approval.
3. Check the data is current and run one cycle by hand:

```bash
uv run tp-data bars daily
uv run tp-paper propose          # after the close
uv run tp-paper proposals        # what is waiting for you
uv run tp-paper approve --all    # or ids, or --quantity N for fewer shares
uv run tp-paper submit           # before 9:28am ET the next morning
uv run tp-paper sync             # after the open
uv run tp-paper status
```

No keys yet? Add `--broker fake` (or `TP_PAPER_BROKER=fake`) to any command: a simulated
account fills market-on-open orders at the lake's opening prices, so the whole cycle runs
offline.

## Approval

Every proposal shows the order, its estimated value, the strategy's reason ("SPY month-end
close 512.30 is above its 10-month average 498.12: invested") and every risk check it passed.

- `approval = "manual"` (default): proposals wait for you. You can approve fewer shares than
  proposed, never more (more would skip the risk checks). Anything undecided at 09:10 expires,
  and the record shows it as missed.
- `approval = "auto"`: proposals that pass risk are approved as proposed. Allowed for paper only;
  the plan keeps a human on every live order.

Every decision is stored with who made it and when, which later lets you measure whether your
overrides helped or hurt.

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
