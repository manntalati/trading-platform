# Strategy 14: timing by a bred rule

A rule from the grammar search (`tp-backtest search --families grammar`), traded on the engine
so it can go through the same gate as any other strategy. The search breeds the rule; this
strategy only runs it ([`library/rule_timing.py`](../../strategies/src/tp_strategies/library/rule_timing.py),
[`grammar.py`](../../strategies/src/tp_strategies/grammar.py)).

## Economic rationale

None of its own: that's the point to be careful about. The grammar is built from signals with a
published case (trend: the 200-day average, Faber 2007; time-series momentum, Moskowitz, Ooi
and Pedersen 2012; the moving-average cross; low volatility, Moreira and Muir 2017), and the
search can only combine them. A bred rule earns trust only through the search's guards (the
Deflated Sharpe over every rule ever tried, the probability of backtest overfitting, the
walk-forward of the search) and then this gate. Prefer the simplest rule that does the job: the
search already penalizes size.

## Rules

| | |
|---|---|
| Universe | `assets`: by default the nine diversified ETFs of strategy 2 (SPY, EFA, EEM, IEF, TLT, HYG, VNQ, DBC, GLD) |
| Signal | `rule`, e.g. `and(ma(200),mom(126))`: `ma(N)` close above its N-day average; `mom(N)` N-day return above zero; `cross(F,S)` F-day average above the S-day; `calm(N)` N-day volatility below its median over the past year; `rsi(N,K)` RSI(N) above K; combined with `and`, `or`, `not`, at most three levels deep |
| Portfolio | Each asset the rule says yes to at an equal share of the sleeve (one `assets`-th each); the rest in cash |
| Checks | `rebalance`: daily, weekly (the week's last session) or monthly (month end) |
| Execution | Next session's open, 5 bps slippage |

```bash
uv run tp-backtest search --families grammar           # breed rules (opt in: many trials)
uv run tp-backtest validate rule-timing -p 'rule=and(ma(200),mom(126))' -p rebalance=monthly
```

Quote the rule in the shell: it has parentheses.

## Results on your data

| Rule | Search verdict | Validation gate | Notes |
|---|---|---|---|
| _fill in_ | | | |

## Known failure regimes

Whatever its signals miss: trend rules lag at turns and whipsaw in sideways markets; momentum
crashes at sharp reversals; a low-volatility filter can sit out the rebound after a crash. A rule
that worked because of one period (2008, 2020, 2022) is the classic overfit the guards exist for.

## Promotion notes

Paper trade only what passes `tp-backtest validate` (block `[strategies.rule-timing]` in
`config/paper.toml` with `params = { rule = "...", rebalance = "monthly" }`). Searching again
and swapping rules every month is itself overfitting: settle on one, and let the paper gate's
60 days judge it.
