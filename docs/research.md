# Options research: swings and LEAPS

**Research → Options ideas** ranks stocks that set up for an options trade and picks the contract
each approach would use. **Research → Look up a symbol** shows any ticker the same way, and
**Portfolio → Positions** says what you could do with the options and shares you already hold.
Every idea comes from fixed rules (`tp_strategies.options_research`) and shows the numbers behind
it. They're research prompts, not advice: options can lose their whole value, and quickly.

## Which names

Everything with daily bars: your own list (below), the options universe, your holdings, and the
bars universe. Cash, bond and leveraged funds are left out. Contracts are picked only for names
with an option chain from the last five days.

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

## The stock setups

From the daily bars:

| Setup | Rule |
|---|---|
| Uptrend | price above its 50-day average, which is above the 200-day |
| Pullback | an uptrend trading back near its 20-day average (−4% to +1%): a calmer entry |
| Extended | 8%+ above the 20-day average, or up 15%+ in a month: chasing risks a pullback |
| Downtrend | price below the 50-day, which is below the 200-day |
| Relative strength | 3-month return against the S&P 500 (SPY) |

Ranking (percentile ranks within the names scanned, so a score compares names with each other):

- **Swing calls** (uptrends): 6-month momentum, strength against the S&P 500, distance above the
  200-day; a bonus for a pullback, a penalty for extended.
- **LEAPS** (uptrends): 1-year return, distance above the 200-day, and a shallow worst drop over
  the year: steady compounders suit a contract held for a year or two.
- **Bearish puts** (downtrends): the mirror of swing.

## The contracts

Priced from the latest chain at the ask to buy and the bid to sell, with implied volatility
solved from the mid and Black-Scholes greeks:

| Approach | Contract |
|---|---|
| Swing call or put | 30 to 100 days out (nearest 60), delta about 0.55: at or slightly in the money, so the stock doesn't need a big move just to break even; spread under 15% of the price |
| Debit spread | the swing contract, minus the ~0.30-delta one in the same expiry: cheaper and less hurt by a volatility drop, gain capped at the strike sold. Leads instead of the plain call when options are expensive |
| LEAPS call | 9+ months out, delta about 0.80, the strike and expiry paying the least time value per year: a stand-in for 100 shares. Shows what it costs against the shares, the capital it frees, the time value per year (the price of the leverage) and the yearly return the stock needs to break even |

Each pick shows breakeven and the move it needs, the market's odds of finishing at a profit,
delta, time decay per day, the bid/ask spread and open interest, and (in Look up) a scenario grid.

## How expensive are the options?

- **IV rank**: where today's 30-day implied volatility sits in its own history (0% = the low,
  100% = the high). It's built from the daily chain snapshots (`clean/option_iv_daily`) and
  needs 20 days of them; until then, the next measure stands in.
- **Implied vs realized**: 30-day implied volatility against how much the stock actually moved
  over 20 days.
- Options count as **expensive** at IV rank 60%+ or implied 1.3x realized. Then the spread leads
  for swings, and a LEAPS note suggests waiting for calmer pricing.
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

## Asking Claude

Each symbol in **Look up** has a chat with Claude, which gets the setup, option pricing, every
contract the rules picked, and a summary of your portfolio. It's told to check earnings and news
with a web search, compare structures, propose an entry, a target, an exit if wrong and a size,
and say what would invalidate the idea. Setup and costs: [positions.md](positions.md#ask-claude).
