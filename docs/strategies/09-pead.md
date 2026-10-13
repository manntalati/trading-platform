# Strategy 9: post-earnings-announcement drift (the plan's strategy 8)

- Family: event driven, earnings (daily, holding about three months)
- Sources: Ball and Brown, "An Empirical Evaluation of Accounting Income Numbers" (Journal of
  Accounting Research, 1968); Bernard and Thomas, "Post-Earnings-Announcement Drift: Delayed
  Price Response or Risk Premium?" (Journal of Accounting Research, 1989) and "Evidence that
  Stock Prices Do Not Fully Reflect the Implications of Current Earnings for Future Earnings"
  (Journal of Accounting and Economics, 1990). Citations from general knowledge; check the
  details against the papers.
- Code: [`library/pead.py`](../../strategies/src/tp_strategies/library/pead.py)
  (`tp-backtest run pead`)
- Stage: 1 (backtest)

## Economic rationale

After a company reports earnings far above what its own history suggested, its stock keeps
drifting in the direction of the surprise for weeks: prices under-react to earnings news.
Bernard and Thomas show the market behaves as if earnings were a random walk when they are in
fact autocorrelated quarter to quarter (a good quarter predicts good next quarters), so part of
the news is only priced at the following announcements. The drift is strongest in the 60
trading days after the announcement.

## Rules

| | |
|---|---|
| Universe | The 38 large caps |
| Surprise | Standardized unexpected earnings (SUE), seasonal random walk with drift: this quarter's diluted EPS minus the same quarter a year earlier, less the average such change over the previous 8 quarters, over their standard deviation (at least 6 needed). EPS is as first reported and split adjusted (`tp_core.fundamentals.sue`) |
| Event date | The first session the figure is in the data: the day of the 10-Q or 10-K (`tp_strategies.reference`). That is days to weeks after the earnings release (8-K); see below |
| Entry | SUE of 2 or more, the largest first, into at most 10 positions of 10% each |
| Exit | 60 sessions after entry, or when the next results come in with a negative SUE |
| Long only | Negative surprises are not shorted (the risk service blocks shorts); a held name with a negative new SUE is sold |
| Execution | Next session's open, 5 bps slippage |

**The event date is conservative on purpose.** The surprise is computed from the XBRL figures
of the 10-Q, so trading on the earnings release (8-K) date would use a number the data didn't
have yet. In the papers the drift runs for about 60 trading days, so entering at the 10-Q still
leaves most of it; parsing the release itself would capture the first days too, and is left for
later.

## What the sources report (to check against)

| Sample | Top-minus-bottom SUE decile, 60 days after the announcement | Share of the drift in the next announcement's window |
|---|---|---|
| NYSE/AMEX, 1974-1986 (Bernard and Thomas 1989) | _fill in from the paper_ | |

## Known failure regimes

- **Decay since publication**: later studies find the drift smaller in large, liquid stocks and
  in recent decades; the 38 large caps are where it is weakest.
- **Market selloffs**: a long-only portfolio of recent winners falls with the market (2020, 2022).
- **Few events**: 38 names report 4 times a year, about 150 events, of which few have SUE ≥ 2;
  the sleeve is often partly in cash and the statistics are thin.
- **Accounting noise**: one-off items move GAAP EPS without changing the business (a tax
  benefit, a write-down); SUE on GAAP diluted EPS counts them as surprises.
- **Survivorship**: today's large caps had good earnings histories by construction.

## Promotion notes

Needs `tp-data fundamentals` running every weekday evening so a filing reaches the strategy the
evening it is made.
