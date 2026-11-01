# Strategy 15: the first 5-minute candle against the 12 EMA

- Family: intraday momentum, day trade (5-minute bars)
- Sources: the rule as it is shared for Nasdaq-100 futures and QQQ (wait for the open, watch the
  first 5-minute candle, buy if it closes above the 12-period EMA, sell if below); for the
  intraday-momentum reasoning, Gao, Han, Li and Zhou, "Market Intraday Momentum" (Journal of
  Financial Economics, 2018); for picking the tickers, Zarattini, Barbon and Aziz, "A
  Profitable Day Trading Strategy For The U.S. Equity Market" (SSRN, 2024), whose opening-range
  trades earn their edge on "stocks in play", the names with the highest relative volume that
  morning. Citations from general knowledge; check the details against the sources.
- Code: [`library/open_ema.py`](../../strategies/src/tp_strategies/library/open_ema.py)
  (`tp-backtest research open-ema`; research only)
- Stage: 1 (research backtest)

## Economic rationale

The EMA of 5-minute closes runs on across sessions, the way a chart draws it, so at 9:35 it is
mostly where the stock spent yesterday's last hour. A first candle that closes above it says the
overnight news was bought and the open held; below, sold and held. The bet is that the morning's
direction carries on (on SPY, Gao et al. found the first half hour's return predicts the last
half hour's), most of all on days with news and heavy volume, when large orders take the day to
work through and the side caught wrong has to cover. On a quiet day in a liquid index fund the same candle is mostly
noise, which is why the choice of tickers matters as much as the rule.

## Rules

| | |
|---|---|
| Data | Alpaca 5-minute bars, regular hours (`tp-data bars intraday`) |
| Signal | At 9:35 New York: the 9:30 candle's close against EMA(12) of 5-minute closes, continuous across sessions. Above: long; below: short (`--side long` for a cash account) |
| Entry | The open of the 9:35 bar, never the candle's own close, plus 1 bp |
| Stop | The candle's other end (its low for a long), filled 2 bp worse than the level (a stop is a market order once touched); `stop="atr"` (a tenth of the 14-day ATR) and none are tried too |
| Target | None by default; 1R and 2R are tried (R: the risk to the stop) |
| EMA exit | Optional: out on a 5-minute close back across the EMA |
| Flat | At the last bar's close: nothing held overnight |
| Same-bar ambiguity | A bar that opens past a level fills at its open; one that touches the stop and the target counts as stopped |
| Sizing | The day's capital split equally among the day's trades |
| Costs | 1 bp each way (half the spread on a liquid name) plus the stop's 2 bp; `--cost` to raise it |

## Which tickers

The study (`open_ema.study`) runs the same rule on every way of choosing names, on the later
40% of the sessions (the test) after anything chosen has been chosen on the earlier 60%:

| Selection | What it answers |
|---|---|
| The index funds (QQQ, TQQQ, SPY, IWM, DIA) | The Nasdaq version of the rule, as it's usually traded |
| Every name | The rule with no selection at all: the baseline every other row has to beat |
| The best quarter on train | Do the tickers it worked best on keep working? If they do no better than the rest on the test, "the best tickers" were luck |
| Top 5, 10 or 20 by relative volume | Stocks in play: the first bar's volume against its own 14-session average, ranked each morning, above a floor (price $5+, average volume 1M+ shares, ATR $0.50+) |
| Relative-volume quintiles | Whether the result climbs with the morning's volume, as the stocks-in-play finding says it should |
| Gaps of 1%+ | Whether a big overnight move is where the rule earns |

Everything a morning filter reads was known at 9:35: the candle, the previous close, and the 14
sessions before. Compare each row with **every name** and with the controls (always long at
9:35; long or short by the candle's colour): a selection is worth trading only if it beats both
on the test sessions, and the train and test numbers agree.

## How to run

```bash
uv run tp-data bars intraday --universe options --days 1000 --resume   # ~100 names, 4 years
uv run tp-backtest research open-ema                  # the study; kept in reports/research/
uv run tp-backtest research open-ema --symbols QQQ,SPY,TQQQ   # the index-fund version alone
uv run tp-backtest research open-ema --side long --cost 3     # long only, dearer fills
```

## What to check (fill in from your run)

| Sample | Selection | Trades | Mean (bp) | Sharpe | Max drawdown |
|---|---|---|---|---|---|
| test sessions | QQQ | _from your run_ | | | |
| test sessions | every name | | | | |
| test sessions | top 10 by relative volume | | | | |
| test sessions | always long (control) | | | | |

## Before trading it

- **The 9:35 data.** Alpaca's free plan delivers the full consolidated feed (SIP) 15 minutes late;
  in real time it's IEX alone, a few percent of the volume. Prices from IEX are close enough to
  call the candle, but relative volume isn't: picking stocks in play at 9:35 needs real-time SIP
  (Alpaca's paid market-data plan) or another real-time source.
- **The pattern day trader rule.** In a margin account under $25,000, four or more day trades in
  five business days get the account restricted. A cash account has no such limit but can't sell
  short, and a sale's cash settles the next business day before it can buy again.
- **Shorting.** The backtest borrows for free. The names in play are often the hard-to-borrow
  ones; `--side long` shows the rule without shorts.
- **The open is expensive.** Spreads are widest in the first minutes; 1 bp each way is for liquid
  large caps and index funds. Rerun with `--cost 3` or `--cost 5` before believing a small edge.
- **Paper first.** Fidelity is read-only here; nothing places an order on it.

## Known failure regimes

- **Gap and fade**: a strong open that reverses by mid-morning; the stop at the candle's end is
  the whole loss, and a tight candle means a tight stop that noise takes out.
- **Quiet, range-bound days**: the candle's direction is noise and costs win.
- **Earnings and news days in the wrong direction**: the in-play names are in play because of
  news, and news can move against the morning.
- **Short history**: Alpaca's 5-minute bars go back to 2016; a few years of a day-trading rule
  is a modest sample, and the in-play rows trade only a few names a day.

## Promotion notes

Needs intraday execution: a runner that reads the 9:35 candle, places the entry with its stop on
Alpaca paper, and flattens by 15:55. Build it only if the study holds up on the test sessions
across costs, and keep it on paper for the 60-day gate before anything else.
