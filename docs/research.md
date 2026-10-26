# Options research: swings, LEAPS and selling premium

**Research → Options ideas** lists the options trades the playbook's setups call for today, each
with the contract that expresses it best, an entry limit and an exit plan. **Research → Look up a
symbol** shows any ticker in depth, and **Portfolio → Positions** says what you could do with the
options and shares you already hold. Every idea comes from fixed rules (`tp_strategies.setups`,
`tp_strategies.playbook`, `tp_strategies.option_edge`) and shows the numbers behind it. They're
research prompts, not advice: options can lose their whole value, and quickly.

## Which names

Everything with daily bars: your own list (below), the options universe (about 100 liquid names:
index and sector ETFs, mega caps and the most traded single-stock options), your holdings, and
the bars universe. Cash, bond and leveraged funds are left out. Contracts are picked only for
names with an option chain from the last five days, and a name only makes a list with a contract
that fits; at most two names from one sector (or one fund class, for ETFs) make each list, so one
hot industry can't fill it.

### Adding your own tickers

Put them in `config/universes.local.toml` (git-ignored, so your list stays off GitHub):

```toml
[research]
symbols = ["PLTR", "SHOP"]
```

From the next daily runs they get price history (`tp-data bars daily`) and option chains
(`tp-data options snapshot`). To see one right away, open **Look up a symbol**, type it, and
press **Fetch**: that pulls two years of daily bars and today's chain from Alpaca (about a
minute). The daily option snapshot also covers everything you hold, so rolls and covered calls
on your positions are always priced.

## The playbook

The ideas used to be the strongest trends ranked by momentum, which change slowly, so the same
names topped the lists day after day. Now an idea needs a **setup that just fired**, and its
contract is chosen by what it's expected to make, so the lists turn over as setups fire and
expire, and the options' price decides as much as the chart.

### Setups: when to get in

From the daily bars (`tp_strategies.setups`):

| Setup | List | Fires when | Horizon | Out on the stock when |
|---|---|---|---|---|
| Pullback in an uptrend | swing | RSI(2) under 10, above the 200-day, 50-day above the 200-day | 10 sessions | a close above the 5-day average |
| Breakout from a squeeze | swing | a new 50-day closing high above the 200-day, within 5 sessions of the tightest Bollinger bandwidth of 6 months | 20 | a close below the 10-day |
| Earnings beat | swing | results with a surprise (SUE) of +1.5 or more, the stock up over the 10 sessions to their filing | 40 | a close below the 50-day |
| Trend leader at its 50-day | LEAPS | 12-1 month momentum of 10%+ (top 40% of the names), within 15% of the 52-week high, back near its 50-day | 126 | a close below the 200-day |
| Bounce in a downtrend | bearish | RSI(2) over 90 below the 200-day, 50-day below the 200-day | 10 | a close below the 5-day |
| Breakdown to a new low | bearish | a new 50-day closing low below the 200-day | 20 | a close above the 10-day |
| Earnings miss | bearish | SUE of -1.5 or less, the stock down into the filing | 40 | a close above the 50-day |

And from the option chain:

| Setup | List | Fires when |
|---|---|---|
| Expensive options: sell premium | income | 30-day implied at least 1.25x the volatility forecast, IV rank 50%+ when known, no event priced before expiry, not in a downtrend |
| Cheap options: buy volatility | volatility | 30-day implied at most 0.9x the forecast, the term structure not inverted, no event priced in |

A signal counts for a few sessions (two for a pullback, ten for a leader) unless its stock-side
exit has come since. Leaders still well above their 50-day are listed under LEAPS as ones to
**wait for**, with the entry level.

### The contract: what the model expects

For each setup every liquid candidate is valued at the setup's horizon (`tp_strategies.option_edge`):
long calls or puts across the expiries that leave at least 21 days at the horizon and deltas from
0.40 to 0.75, debit spreads selling the 0.30- or 0.20-delta strike, LEAPS 9+ months out at delta
0.70-0.85, put credit spreads, cash-secured puts and iron condors 25 to 50 days out (valued at 21
days to expiry, when they'd be closed), and at-the-money straddles. Under the model:

- the stock ends the horizon lognormally around today's price, with **the setup's drift** (what
  stocks did after the same signal in the backtest; with no calibration an ordinary 7%-a-year
  market drift, so puts start with the wind against them) and **the volatility forecast** for the
  horizon (HAR-RV for the next month, blended toward the year's realized for longer horizons);
- each leg is valued with the implied volatility today's surface gives an option of the time it
  will have left and the moneyness it will have (the skew is fitted on out-of-the-money options,
  weighted by vega), drifting back toward the stock's usual implied volatility once there is a
  year of history;
- legs are bought at the ask and sold at the bid, closed across half the spread, $0.65 a
  contract each way.

The pick is the candidate with the best expected return per unit of its spread of outcomes, less
a penalty for wide quotes. So cheap options favour a plain call and expensive ones a spread (the
sold leg pays for the volatility); a stock with a great chart and overpriced options ranks below
one with a decent chart and cheap ones. Each pick shows:

- **Expected** return on the capital at risk over the horizon, the odds of a profit, and the
  worst tenth of outcomes;
- the **greeks** of the position: delta in shares, gamma, theta a day (and as a share of the
  premium), vega a volatility point;
- **gamma against theta**: the daily move the stock needs for its gamma to pay a day's decay,
  next to the move it's forecast to make (`sqrt(2 * theta / gamma)`);
- the **entry**: a limit at the middle of the bid and ask, the most worth paying (the modeled value
  at the horizon; past it the expected return turns negative), and the last session the signal
  counts;
- the **exit plan**: a profit target and a stop on the premium (for a credit: keep 50% of it, stop
  at a loss of twice it), the stock-side rule with today's level, a time stop, and the date to be
  out by (21 days before expiry for swings, 180 for LEAPS: the decay speeds up after).

### Sized to your account

Ideas only use contracts you can actually buy, from the latest brokerage sync
(`tp_strategies.budget`):

- **The account**: a position lives in one account, so ideas are sized for one: the one
  `TP_OPTIONS_ACCOUNT` names in `.env` (its name, or the last 4 digits of its number), else the
  one with the most buying power. The Alpaca paper account counts only when nothing else is
  linked.
- **Buying power**: as Fidelity reports it (cash available to trade now; purchases that haven't
  settled are already out of it), or cash plus money-market funds when it isn't reported.
- **What each candidate needs**, per contract: the debit for anything bought, what the broker
  holds against a credit spread or an iron condor (the most it can lose), the strike x 100 for a
  cash-secured put. Candidates that need more than the buying power are left out **before** the
  best is chosen, so you get the best contract that fits: a cheaper strike, a spread instead of a
  single call, or a nearer expiry. Among those, the ones that keep the most one contract can lose
  within `TP_OPTIONS_RISK` of the account's value (2% by default) come first.
- **Size**: as many contracts as the buying power pays for and the risk guideline allows; if
  even one contract risks more than the guideline, it's one, flagged.
- **Options level**: set `TP_OPTIONS_LEVEL` to the level your broker approved (1 covered calls; 2
  also buying calls and puts, straddles and cash-secured puts; 3 also spreads and iron condors)
  and nothing above it is suggested.
- Setups that fired where nothing fits are listed under each list as **out of reach**, with what
  the cheapest fit would need.

A new sync (hourly on trading days) makes a new set of ideas, so they follow your cash.

The defaults for drift and exits are rules of thumb until the backtest has run; then each setup
gets the drift its signal actually had and the exits that held up best out of sample (below,
[What to run](#what-to-run)). One idea per name per list, at most two names per sector or fund
class, best first.

## What to run

Nothing needs training by hand; the backtest calibrates itself. Once, after updating:

```bash
uv run tp-data bars daily          # backfills five years for the ~80 names new to the options list
uv run tp-data options snapshot    # chains for all ~100 names (market hours; a few minutes)
uv run tp-recs calibrate           # backtest every setup: its drift and its best exits (a minute or two)
uv run tp-data options history     # real daily bars of the contracts those trades used since Feb 2024
uv run tp-recs calibrate           # again: now checked against the real prices
uv run tp-recs run                 # the ideas with the calibration (or Update now on the page)
```

After that the scheduler keeps it current: the chains and bars every trading day, past option
prices on Saturdays from 08:00, the calibration from 09:00 (`tp-jobs status` shows both), and a
new set of ideas whenever any of them lands. `tp-data fundamentals` (daily, already scheduled)
is what the earnings setups read.

`tp-recs calibrate` prints, per setup: how many times it fired, how often the stock went its way
over the horizon and by how much (against all names over the same horizon), the drift the live
model will use, and the option trade's result on the later 40% of the signals (the ones the
exits weren't chosen on) with the chosen exits and with the defaults. With real prices it also
says how the same trades did on them and how far the model was off. What the backtest can't
see: the modeled trades assume implied volatility moves with realized volatility, so they miss
IV crushes and spikes (the real-price trades don't); and the names are today's liquid ones, so
it overstates what the setups did on the names as they were at the time.

What to trust, in order: the track record's **following the plan** column (real quotes, real
recommendations, but only as many as the weeks it has run), then the real-price backtest (real
contracts, from February 2024), then the modeled backtest (years of history, a model of the
options). A setup that looks good only on the last of these is a guess.

## The contracts on Look up

**Look up** shows, besides the playbook's view of the name (the setups live on it, how close the
others are), one contract per approach by fixed rules, priced from the latest chain at the ask to
buy and the bid to sell, with implied volatility solved from the mid and Black-Scholes greeks:

| Approach | Contract |
|---|---|
| Swing call or put | 30 to 100 days out (nearest 60), delta about 0.55: at or slightly in the money, so the stock doesn't need a big move just to break even; spread under 15% of the price |
| Debit spread | the swing contract, minus the ~0.30-delta one in the same expiry: cheaper and less hurt by a volatility drop, gain capped at the strike sold. Leads instead of the plain call when options are expensive |
| LEAPS call | 9+ months out, delta about 0.80, the strike and expiry paying the least time value per year: a stand-in for 100 shares. Shows what it costs against the shares, the capital it frees, the time value per year (the price of the leverage) and the yearly return the stock needs to break even |
| Cash-secured put | sell the ~0.25-delta put 25 to 50 days out (nearest 35), with the cash for 100 shares set aside: the premium and its yield (and per year), the price you'd own the shares at if assigned, and the odds of that |
| Put or call credit spread | sell the ~0.30-delta contract, buy back the ~0.15-delta one further out, same expiry: the credit, the most it can lose (the width less the credit), the return on that risk, breakeven and odds. A put spread is bullish, a call spread bearish |
| Iron condor | a put credit spread and a call credit spread on one expiry, shorts about 0.20 delta, wings about 0.10: paid while the stock stays between the short strikes; the profit range, its odds and the maximum loss |
| Calendar | sell the ~30-day at-the-money option, buy the same strike ~75 days out: paid by the front month's faster decay while the stock sits near the strike, and by a rise in implied volatility. The best case (at the strike when the front expires, priced at the back month's implied volatility), the range that makes money and its odds |

Each pick shows breakeven and the move it needs, the market's odds of finishing at a profit,
delta, time decay per day, the bid/ask spread and open interest, and (in Look up) a scenario grid.
Sold contracts are priced at the bid, so the credit is what you'd actually get. Selling premium
caps the gain at the premium while the loss can be many times it (all the way down to zero for a
cash-secured put): size by the maximum loss, not by the premium. Implied volatility is solved
with Newton's method (bisection when it won't converge); a deep in-the-money put that Black-Scholes
can't price is read with the American model.

## Earnings and valuation

Look up shows what the company's SEC filings say ([data.md](data.md#fundamentals-sec-edgar)):
when results last came out and an estimate of the next date (52 weeks after the same quarter's
release a year earlier), the last quarter's surprise against the seasonal trend (SUE, in
standard deviations), trailing P/E, book to market, return on equity and gross profit over
assets. The next date is an estimate: confirm it (the Claude chat searches for it) before
holding options through it.

## How expensive are the options?

- **IV rank**: where today's 30-day implied volatility sits in its own history (0% = the low,
  100% = the high). It's built from the daily chain snapshots (`clean/option_iv_daily`) and
  needs 20 days of them; until then, the next measure stands in.
- **Implied vs forecast**: 30-day implied volatility against a *forecast* of how much the stock
  will move over the next month (HAR-RV from its daily ranges, [ml.md](ml.md#volatility-forecasts)).
  An option seller is paid for implied minus the volatility still to come, not the volatility
  already past. After a quiet month the trailing number understates what's coming, and implied
  looks cheap against the forecast when it looked rich against the past.
- **Implied vs realized**: the same against the last 20 days' moves, shown alongside. It stands
  in when there's less than about 10 months of bars to forecast from.
- Options count as **expensive** at IV rank 60%+ or implied 1.3x the forecast (or realized, with
  no forecast). Then the spread leads for swings, and a LEAPS note suggests waiting for calmer
  pricing.
- **Skew** (`tp_core.smile`): each expiry's smile is fitted with SVI, and the 25-delta risk
  reversal (the 25-delta call's implied volatility minus the put's) read off the fit. For stocks
  it's negative: protection costs more than upside. Steeply negative skew pays put sellers well
  and makes buying puts dear; the ideas say so. Look up shows the fit, its error and any
  arbitrage in the smile (a negative density, or less total variance than an earlier expiry), and
  counts call quotes that contradict each other (usually stale quotes).
- **Term structure**: the 90-day at-the-money implied volatility over the 30-day. Above 1
  (contango) is the calm norm; below 1 (backwardation) the market expects a rough few weeks.
- **Events**: when one expiry's at-the-money IV stands well above the expiries either side, the
  market is pricing a jump (usually earnings) before it. Options held through it usually lose
  value when it passes (the "IV crush"), even if the stock moves the right way. The idea says so.

## What you could do with what you hold

On each position under **Portfolio → Positions**, priced from the latest chain:

| Holding | When | Suggestion |
|---|---|---|
| Long option, under 90 days | up 50%+ | take some off (selling half returns about …) |
| | 21 days or less, mostly time value | close or roll out to ~60 days at the same strike (net cost, new decay per day) |
| | down 50%+ with under 20% odds | low odds of recovering: what selling salvages |
| Long option, 90 to 180 days, delta 0.6+ | a long-term holding getting short on time | roll out to the longest LEAPS expiry (net cost) |
| LEAPS, 180+ days | up 100%+ and delta 0.85+ | roll up to a ~0.70-delta strike, taking cash off the table |
| 100+ shares | always | covered call (~35 days, delta ~0.25): premium, yield, the cap, odds of assignment; and the LEAPS that would replace 100 shares |
| A large winner (15%+ of the account, up 50%+) | always | a protective put (~75 days, delta ~0.25) and what it costs |
| Shares below the 200-day and below cost | always | the long-term trend is against it |

Home lists the urgent ones (exercise at expiry, low odds, decisions before decay speeds up).

## Kept, refreshed and scored

The ideas are made on a schedule and kept. The job scheduler in `tp-api` makes a new run whenever
new data lands (the midday and closing option chains, the evening bars, a Fidelity sync, macro
data), and **Update now** makes one on the spot; opening the page reads the latest run at once.
Each run is stored (`reports/recommendations/<run>.json` for 30 days, and one line per idea in
`clean/recommendations_log` for good), so:

- **New** marks an idea that wasn't on the same list at the previous session's last run, and each
  list says which names dropped off since.
- **Research → Track record** shows how every idea has done since it was first listed: the
  stock's move in the recommended direction (a fall counts for a bearish idea) after 5 and 21
  sessions and to date, against the S&P 500 over the same time, and the position itself marked at
  the middle of the latest bid and ask, or at its value at expiry. Entries are the prices the
  pick was made at (the ask to buy, the bid to sell), so the record is no kinder than a real fill.
  For premium selling it's the share of the premium kept. Per list: how many ideas, how many are
  profitable, the median result, how often the stock went the right way, how often it beat the
  S&P 500.
- **Following the plan**: each idea's exit plan is followed through the daily chain snapshots, and
  the position closed at the first session its target, stop, stock-side rule, time stop or
  days-to-expiry limit was reached (or valued at expiry). That's what following the plans would
  have made, per list and per setup, on real quotes: the live check on the backtest.

From the terminal: `uv run tp-recs run` (make one now), `tp-recs show` (the latest, ★ for new,
with each idea's entry and exit plan) and `tp-recs scorecard` (the track record, with what
following the plans made). A track record of rules applied to past data is
evidence about the rules, not a promise; a few weeks of it is mostly noise.

## Asking Claude

Each symbol in **Look up** has a chat with Claude, which gets the setup, option pricing, every
contract the rules picked, and a summary of your portfolio. It's told to check earnings and news
with a web search, compare structures, propose an entry, a target, an exit if wrong and a size,
and say what would invalidate the idea. Setup and costs: [positions.md](positions.md#ask-claude).
