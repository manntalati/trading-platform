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
        if not ctx.is_last_session_of_month():
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
- Reference data comes through the context too: `ctx.sector(symbol)` gives the GICS sector from
  `config/classifications.toml`.
- Size inside the risk limits rather than relying on rejections: `order_target_weights` already
  never borrows and never leaves a trimmed position above its target.
- Write the spec in `docs/strategies/` before the code.
