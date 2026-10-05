# Research ideas

`tp-ideas` (and the dashboard's Ideas page) turns your synced portfolio and the market data in
the lake into a short list of things worth a look. Every idea comes from a fixed rule, and the
idea itself shows the numbers that triggered it.

> These are prompts for your own research, not recommendations. Rules can't know your goals,
> taxes or time horizon; backtests are hypotheses. The ground rules still apply: the platform
> proposes, you decide, and nothing here can place an order.

```bash
uv run tp-ideas                   # everything, most urgent first
uv run tp-ideas --kind candidate  # holding | portfolio | strategy | candidate
```

## Rules

| Kind | Rule | Severity |
|---|---|---|
| holding | Last full month closed below the 10-month average (strategy 1 would be out) | attention |
| holding | More than 25% below the 52-week high | consider |
| holding | 100+ shares of an optionable name (covered-call eligible) | info |
| portfolio | A single stock above 10% of the portfolio (funds exempt) | attention |
| portfolio | A sector above 30% | consider |
| portfolio | More than 80% US equity with no bonds, international or real assets | consider |
| portfolio | Beta to the S&P 500 above 1.2 over the last year | consider |
| portfolio | More than 5% of the portfolio not priceable (mutual funds, options) | info |
| strategy | 10-month MA timing as an overlay on your current weights: suggested when it would have cut the max drawdown by 5+ points while costing at most 2 points of CAGR | consider / info |
| strategy | Faber's 5-asset GTAA as a diversifier, when the mix is mostly US equity | consider |
| candidate | Not held, above its 10-month average, positive 12-1 momentum; scored 50% momentum rank, 30% low correlation to your portfolio, 20% filling a sector under 10% | info |

Thresholds live in `tp_strategies.ideas.Limits` and mirror the risk limits in the project plan.

## Known limits

- Candidates come only from symbols with bars in the lake (the universe plus your holdings), and
  the universe is today's large caps (survivorship bias, see [data.md](data.md)).
- Momentum and trend are price-only. No fundamentals, earnings dates or news yet.
- The overlay backtest uses today's weights for the whole history; it describes the mix, not your
  past decisions.
