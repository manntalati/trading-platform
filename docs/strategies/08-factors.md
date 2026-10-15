# Strategy 8: factor portfolios: value, quality, low volatility (the plan's strategy 7)

- Family: cross-sectional factors (monthly)
- Sources: Fama and French, "The Cross-Section of Expected Stock Returns" (Journal of Finance,
  1992) for value (book to market) and earnings yield; Novy-Marx, "The Other Side of Value: The
  Gross Profitability Premium" (Journal of Financial Economics, 2013) for quality; Ang, Hodrick,
  Xing and Zhang, "The Cross-Section of Volatility and Expected Returns" (Journal of Finance,
  2006) and Frazzini and Pedersen, "Betting Against Beta" (2014) for low volatility. Citations
  from general knowledge; check the details against the papers.
- Code: [`library/factors.py`](../../strategies/src/tp_strategies/library/factors.py)
  (`tp-backtest run factor -p factor=value`)
- Stage: 1 (backtest)

## Economic rationale

- **Value**: cheap stocks (high earnings or book value per dollar of price) have earned more
  than expensive ones over long histories. Either they are riskier in ways that matter in bad
  times (distress), or investors extrapolate bad news too far and overpay for glamour.
- **Quality**: among companies at similar prices, the more profitable ones (gross profit over
  assets, the cleanest measure of economic profitability) have earned more. Investors underprice
  durable profitability, and it hedges value's exposure to distress.
- **Low volatility**: the least volatile stocks have earned about as much as the most volatile
  ones with much less risk. Leverage-constrained investors chase volatile stocks for their
  upside, which leaves the quiet ones cheap.

## Rules

| | |
|---|---|
| Universe | The 38 large caps |
| Data | Fundamentals from SEC filings, point in time: each figure only from the day it was filed, as first reported (`tp-data fundamentals`; `tp_core.reference`) |
| Value | Average of the percentile ranks of earnings yield (trailing net income over market cap) and book to market (equity over market cap); negative equity excluded |
| Quality | Gross profit (trailing twelve months) over total assets; return on equity where gross profit isn't reported (banks) |
| Low volatility | Lowest standard deviation of daily returns over the last 252 sessions |
| Composite | Average of the three ranks |
| Portfolio | The 10 best names, equal weight (10% each, the position cap), at most 3 per sector; rebalanced on the last session of each month |
| Execution | Next session's open, 5 bps slippage |

Market cap is shares outstanding (as reported) times the raw close, with the shares carried
across any split since they were reported. Low volatility needs prices only and runs without
fundamentals.

## What the sources report (to check against)

| Factor | Sample | Long-short spread | Long-only top quintile vs market |
|---|---|---|---|
| Value (HML) | US, 1963-1990 | _fill in from Fama-French_ | |
| Gross profitability | US, 1963-2010 | _fill in from Novy-Marx_ | |
| Low volatility | US, 1963-2000 | _fill in from Ang et al._ | |

## Known failure regimes

- **Value's lost decade**: 2010 to 2020, growth stocks led for years; value long-only lagged the
  index badly.
- **Junk rallies**: early 2009 and late 2020, the cheapest and least profitable stocks rebounded
  hardest; quality and low volatility lag.
- **Rate shocks**: low-volatility stocks behave like bonds (utilities, staples) and fell in 2022.
- **A tiny, hindsight universe**: ranking 38 of today's winners is not ranking the market; the
  factor spreads in the papers come from thousands of stocks including the ones that failed.
  Treat results as plumbing tests, not evidence.
- **Fundamental history**: XBRL figures start in 2009 and tags differ between companies, so some
  names lack a ratio in some months (they're left out of that month's ranking).

## Promotion notes

Low volatility can run on the bars alone and is the one to try first. Value and quality need the
fundamentals job running daily so new filings are in before month end.
