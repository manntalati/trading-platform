# Strategy 6: Leveraged ETF momentum rotation (high risk)

- Family: short-term momentum / trend following, **daily**, through 3x leveraged funds
- Source: no single paper. It combines short-horizon time-series momentum (see strategy 2) with
  daily-reset leveraged ETFs. On how those funds compound, see Minder Cheng and Ananth
  Madhavan, *The Dynamics of Leveraged and Inverse Exchange-Traded Funds*, Journal of
  Investment Management (2009). That citation is from general knowledge, so check the paper
  itself.
- Code: [`library/leveraged_momentum.py`](../../strategies/src/tp_strategies/library/leveraged_momentum.py)
  (`tp-backtest run leveraged-momentum`)
- Stage: 2 (paper), as the book's deliberately high-risk sleeve

## Why it exists

The other five strategies are slow and diversified: most trade monthly and hold several assets
unlevered. This one sits at the other end. It trades every day, holds at most two positions,
and gets about 3x market exposure from leveraged funds. It is here to show, on paper, how an
aggressive daily strategy behaves next to the patient ones: the size of its swings, how often
it whipsaws, and what costs and gaps do to it. It is not a candidate for real money as it
stands (see the promotion notes).

## Economic rationale

Over days to weeks, strong recent performance in an index or sector tends to persist a little,
for the same under-reaction and herding reasons behind slower momentum. Daily-reset 3x funds
amplify those runs, which is the appeal. They also amplify every reversal. In a choppy market
they lose value even when the index ends flat (volatility decay), because each day's move is
multiplied and then compounded. The trend filter is there to stay out of the falling and choppy
stretches. Whether it does that well enough is the open question.

Who is on the other side: mean-reversion traders and liquidity providers, who are paid for
taking the other side of short-term moves. Short-horizon momentum is much weaker and less
reliable than 12-month momentum, so expect a low hit rate and long losing streaks.

## Rules

| | |
|---|---|
| Universe | 3x daily leveraged ETFs: TQQQ (Nasdaq-100), UPRO (S&P 500), SOXL (semiconductors), TNA (Russell 2000), FAS (financials), TMF (20+ year Treasuries) |
| Signal | Every session's close: 10-session return; a fund qualifies if that return is positive **and** it closes above its 20-session average |
| Selection | The two qualifying funds with the highest 10-session return |
| Sizing | 50% of the sleeve each; if only one qualifies, half the sleeve is cash; if none qualify, all cash |
| Rebalance | **Daily**, back to 50/50, skipping trims under 0.5% of the sleeve. It trades on most sessions, from rotations and from re-weighting after each day's moves |
| Leverage and shorts | No borrowing and no shorts. The leverage comes from the funds themselves (about 3x the daily index move) |
| Stops | None besides the trend filter and the sleeve's drawdown limit |
| Execution | Next session's open (market-on-open), 5 bps slippage |

Parameters: `lookback` (10), `trend` (20), `top_n` (2), `min_change` (0.005), `assets`. For
example `-p top_n=1` puts everything in one fund. That is even riskier.

## Risk limits

It passes the same pre-trade checks as every strategy ([risk.md](../risk.md)). Some apply
differently because of what it holds:

- **Funds are exempt from the single-name and sector caps**, so 50% in one fund is allowed. The
  gross-exposure check counts the funds' market value. A sleeve that is 100% invested in 3x funds
  therefore passes it, even though its economic exposure is about 300%. That is the deliberate
  exception to the plan's "no leverage" rule. It applies to this paper sleeve only, through
  funds, never through margin.
- **Drawdown limit 50%** (`config/risk.toml`), against 10% for the others. 3x funds routinely
  fall 30% or more in an ordinary correction, so 10% would switch it off within weeks. Reaching
  50% means it is broken.
- **Daily loss limit**: in paper trading this is 2% of the whole account. A very bad day for this
  sleeve, alone or together with a market sell-off, can trip it, and then no sleeve buys that
  evening. In a backtest the limit applies to the sleeve itself, so the backtest skips the buys
  after its own −2% days. That makes the backtest somewhat more conservative than paper trading.

## What to expect

On synthetic data at 3x-fund volatility (about 60% a year per fund), the tests see orders on
about 70% of sessions, annual volatility around 40%, and drawdowns around 40% within two
years. Run it on real bars before reading anything into it:

```bash
uv run tp-backtest run leveraged-momentum --start 2015-01-01
```

The real funds have had much deeper drawdowns: TQQQ fell roughly 80% in 2022, and SOXL by
about 90%. The trend filter should cut that, but it won't remove it.

## Known failure regimes

- **Choppy, sideways markets** (2015–16, much of 2022): the 10-day signal flips often, every flip
  costs a round trip, and volatility decay eats the funds.
- **Gap-downs and V-shaped crashes** (February 2018, March 2020): the signal only reacts after
  the close, and the fill is at the next open. A 3x fund can lose 20–35% in a day before the
  strategy can act.
- **Stocks and bonds falling together** (2022): TMF is the only diversifier, and it fell with
  everything else.
- **Fund risk**: leveraged funds can close or reverse-split, and their daily reset means
  multi-day returns differ from 3x the index's.

## Promotion notes

Paper only. It reaches the paper gate's 30 trades within weeks, but no amount of paper
performance takes it to real money without revisiting the plan's rules (no leverage, 10%
drawdown). What paper trading should answer: how often it trades, how big its swings are, how
much slippage at the open costs at this turnover, and whether the trend filter kept it out of
the worst stretches.
