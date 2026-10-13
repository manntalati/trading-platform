# Backtesting

Two engines, as the plan prescribes:

- **Research (vectorised)**: functions like `tp_strategies.ma_timing.ma_timing` that take a price
  matrix and return daily returns in milliseconds. For sweeps and notebooks.
- **Production-faithful (event-driven)**: `tp_trading.engine.BacktestEngine`. It replays bars one
  session at a time through a `Strategy` object. **The same object runs in paper trading**, so
  paper results test the backtest itself, not a second copy of the logic.

The two must agree: strategy 1 run through the event engine with research settings reproduces
the vectorised returns to 1e-12 (`strategies/tests/test_library.py`). That parity test is what
says the engine's accounting is right.

## Run one

```bash
uv run tp-backtest list                                   # strategies and their parameters
uv run tp-backtest run ma-timing                          # GTAA, from the first bar
uv run tp-backtest run ma-timing -p assets=SPY --start 2018-01-01
uv run tp-backtest run ma-timing --slippage-bps 15        # stress the cost assumption
uv run tp-backtest run ma-timing --fill next_close        # research timing
uv run tp-backtest run ma-timing --sizing vol-target      # size positions (risk.md)
uv run tp-backtest sizing ma-timing                       # every sizing method side by side
```

The command prints the plan's tear sheet next to buy-and-hold of the benchmark (`--benchmark`,
SPY by default), plus trade count, turnover, exposure and costs. The pre-trade risk limits apply
by default ([risk.md](risk.md)); rejected orders are listed with their reasons, and `--no-risk`
runs without them for comparison. Each run is saved under
`data/reports/backtests/<strategy>/<run id>/`:

| File | What |
|---|---|
| `summary.json` | statistics, parameters, engine settings, data version (rows, sessions, latest ingest), git commit and whether the tree was dirty |
| `equity.parquet` | equity, daily returns and benchmark returns per session |
| `orders.parquet` | every order intent: filled, partial, expired, rejected (with the reason) or still pending |
| `fills.parquet` | price, fees and slippage of every fill |

## How a session is simulated

```
session t
  1. orders approved at t-1's close fill   (open of t by default; the close with --fill next_close)
  2. portfolio marked at t's close
  3. strategy.on_bar(ctx)                  ctx.history() ends at t; nothing later exists
  4. risk gate reviews the intents         rejected ones are recorded with reasons
  5. approved orders wait for t+1
```

A signal therefore never trades on its own bar, and `ctx.history()` cannot return a later
session (tested, together with "rewriting future prices never changes earlier orders").

## Validation: the backtest gate

A good backtest is a hypothesis. Before a strategy earns paper money, the plan's stage-1 gate asks
five questions, and `tp-backtest validate` answers them:

```bash
uv run tp-backtest validate ma-timing      # one strategy (its paper parameters, if it paper-trades)
uv run tp-backtest validate --all          # every strategy; exits 1 unless all pass
```

It backtests the strategy's parameter variants (`[variants]` in `config/validation.toml`) through
the same engine, costs and risk limits as `tp-backtest run`, then checks:

| Check | Passes when | Why |
|---|---|---|
| **Locked test set** | Sharpe above 0 after costs on the data after `validation_end` | the only data nothing was tuned on. Every look is counted and shown: look often and it becomes another validation set |
| **Walk-forward** | the stitched out-of-sample returns have a positive Sharpe | every six months it re-picks the variant with the best Sharpe over the last two years and trades it for the next six: what you'd have earned choosing as you went |
| **Deflated Sharpe** | the Sharpe beats what the best of every variant ever tried would show by luck | try 20 versions and the best looks good by chance alone (Bailey and López de Prado). Every variant run is logged in `reports/validation/<strategy>/trials.jsonl` and counts |
| **Stress periods** | in each crash in the data, the drawdown stays under 15% or is no worse than SPY's | COVID 2020, the 2022 rate shock, Aug 2024 (`[stress]`); periods before the data are skipped, so backfill more years to include them |
| **Parameter stability** | every nearby variant keeps at least half the Sharpe | a result that falls off a cliff one step away from the chosen parameters is a lucky spike |

Also reported, not gated:
- **Returns by market regime** (`tp_core.regime`): trend (SPY vs its 200-day), volatility (the VIX: calm under 15, stressed over 25; SPY's 20-day realized volatility with 12% and 25% when the FRED data isn't there), breadth (share of the universe above its 50-day), and with the FRED data, rates (the 10-year yield against its 200-day average) and the yield curve (10-year minus 3-month) ([data.md](data.md#macro-series-fred)). Each day's return goes to the regime known at the previous close.
- **Monte Carlo**: 1,000 block-bootstrap resamples of the daily returns: the 5th to 95th percentile CAGR, the bad-tail drawdown and the share of paths that lose money.

Periods: train up to `train_end`, validation up to `validation_end`, locked test after, each from
the first day the strategy held anything (warm-up days would flatter it). Reports are saved under
`data/reports/validation/<strategy>/` and shown under **Paper trading → Strategies and the
backtest gate**, which can also run them. A weekly cron entry keeps them current
([infra](../infra/README.md)).

## Realism

| | Default | Why |
|---|---|---|
| Fill timing | next session's open | a daily strategy decides after the close; a market-on-open order is what paper trading sends |
| Slippage | 5 bps per fill | stands in for half the spread plus impact on liquid ETFs and large caps; daily bars carry no spread |
| Fees | SEC Section 31 and FINRA TAF on sales; no commission | Alpaca stock trading; rates are published values, check them periodically |
| Shares | whole shares | what an opening-auction order trades; `--fractional` to relax |
| Cash | buys cut to available cash, sells first | a cash account: no accidental leverage |
| Shorts | impossible (sells capped at shares held) | the plan forbids margin |
| Prices | split- and dividend-adjusted | total return; paper trading sizes with raw prices instead |
| Limit orders | day orders; fill at the open if better, else at the limit if the bar reaches it | never a better price than the limit |

Not modelled yet: borrow costs (no shorts), delistings (the universe is today's survivors, see
`config/universes.toml`), trading halts beyond "no bar, order expires", and size-dependent
impact.

## Writing a strategy

```python
@dataclass(frozen=True)
class MyStrategy(Strategy):
    name: ClassVar[str] = "my-strategy"
    title: ClassVar[str] = "What it does"
    spec: ClassVar[str] = "docs/strategies/NN-my-strategy.md"

    assets: tuple[str, ...] = ("SPY", "TLT")
    lookback: int = 63

    def symbols(self) -> list[str]:
        return list(self.assets)

    def on_bar(self, ctx: Context) -> None:
        if not ctx.is_rebalance_day():
            return
        closes = ctx.history("close", lookback=self.lookback + 1)
        momentum = closes.iloc[-1] / closes.iloc[0] - 1
        best = momentum.idxmax()
        ctx.order_target_weights(
            {best: 1.0}, reason=f"{best} has the best {self.lookback}-day return"
        )
```

Rules:

- Fields are the parameters; `tp-backtest run my-strategy -p lookback=126` converts strings to
  the field types. Register the class in `tp_strategies/library/__init__.py`.
- Read the world only through `ctx`: `history`, `price`, `positions`, `equity`, `cash`.
- Ask for trades with `ctx.order(symbol, ±shares)` or `ctx.order_target_weights({...})`. Both
  produce intents; whether they become orders is decided outside the strategy.
- Give every order a `reason`. In paper trading it is what you read before approving it.
- Keep no state between bars: paper trading starts a fresh process each day.
- A monthly strategy checks `ctx.is_rebalance_day()` rather than `is_last_session_of_month()`:
  it is the month's last session in a backtest, and also any session on which a new paper
  sleeve hasn't traded yet, so the sleeve takes its positions straight away instead of sitting
  in cash until month end. The signal must still make sense on that day (MA timing, for one,
  uses the latest completed month).
- Reference data comes through the context too: `ctx.sector(symbol)` gives the GICS sector from
  `config/classifications.toml`.
- Size inside the risk limits rather than relying on rejections: `order_target_weights` already
  never borrows and never leaves a trimmed position above its target.
- Write the spec in `docs/strategies/` before the code.
