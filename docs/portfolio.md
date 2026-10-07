# Portfolio tracking

Your real brokerage account (Fidelity) shown next to the platform's own data, measured against
benchmarks. **Read-only:** nothing in `tp_broker` can place, change or cancel an order.

## Link Fidelity (once)

Fidelity has no public API for individuals, so the link goes through
[SnapTrade](https://snaptrade.com), a read-only aggregator. You log in to Fidelity on SnapTrade's
connection page; this project never sees your Fidelity password.

1. Create a SnapTrade account at <https://dashboard.snaptrade.com> and generate a **personal**
   API key (client id + consumer key). Check their current pricing for personal use.
2. Put the key in `.env` (git-ignored):
   ```
   SNAPTRADE_CLIENT_ID=...
   SNAPTRADE_CONSUMER_KEY=...
   ```
3. `uv run tp-broker link` prints a connection-portal URL (valid 5 minutes). Open it, pick
   Fidelity, choose **read-only** access, and log in.
4. `uv run tp-broker sync` stores the first snapshot; `uv run tp-broker show` prints it.
5. Enable the sync timer (`tp-broker-sync`, hourly on weekdays; see [infra/](../infra/README.md)).

No keys yet? `uv run tp-broker sync --source fake` loads a Fidelity-shaped demo portfolio, and
`--source alpaca` shows your Alpaca paper account.

## What gets stored

Under `data/raw/broker/` (git-ignored, never pushed):

| Dataset | One row per | Notes |
|---|---|---|
| `accounts` | account per sync | cash, broker-reported total; account number masked to last 4 digits |
| `holdings` | position per sync | quantity, broker price, market value, average cost per unit, kind, underlying; options are stored per contract (price and cost x100) |
| `activities` | transaction | buys, sells, option opens/closes/expirations, dividends, contributions, withdrawals, fees; re-fetched with a 30-day overlap, de-duplicated by id |

Every market-priced holding (stocks, ETFs) is added to the daily bars job automatically, so
holdings outside `config/universes.toml` get price history too. Mutual funds (e.g. FXAIX) are not
on Alpaca's market data, so they are valued at the broker's price but left out of the
price-based analytics (the dashboard shows how much of the portfolio that covers).

## How fresh is it?

Fidelity data reaches the platform through SnapTrade, which keeps its own copy:

| | SnapTrade refreshes it | So on the platform |
|---|---|---|
| **Positions and balances** | once a day (around the close), or on demand | values move live all day (prices stream); quantities change after a sync that has newer positions |
| **Transactions** | once a day, and never intraday: a trade appears the **next day** | today's trades show on the Trades tab as **pending**, read off the change in your positions, until the transaction posts |

- `tp-broker sync` copies whatever SnapTrade has; it's free, so run it often (hourly in the
  schedules in [infra/](../infra/README.md)) to pick up SnapTrade's daily refresh soon after
  it lands.
- **Refresh from Fidelity** (the button on the Portfolio and Trades tabs, or
  `tp-broker sync --refresh`) first has SnapTrade re-pull your positions from Fidelity, which
  takes up to a few minutes. SnapTrade charges a small fee per refresh (see the billing page
  of your SnapTrade dashboard), so it's on demand rather than scheduled.
- The tabs say how current the data is: when positions were last pulled from Fidelity, the
  last day of transactions, and when the platform last synced.
- A pending trade has the quantity and, for a buy, the price paid (from the change in the
  position's cost). Its realized P&L comes with the real transaction the next day.

## Up or down: profit and loss

The Portfolio tab answers "how much am I up or down" three ways:

| | What | From |
|---|---|---|
| **Unrealized** | open positions against what they cost | the broker's average cost per position (each sync) |
| **Realized** | what closed trades made or lost | your transactions, by average cost |
| **Total** | unrealized + realized + dividends and interest, less account fees | both |

Realized P&L is worked out from the synced transactions (`tp_core.pnl`): each buy adds its cost
(fees included), each sale realizes the cash received less the average cost of what was sold.
The Trades tab lists every trade with the figure it realized.

- **Options** count contracts and use the cash that changed hands, so the x100 multiplier is
  already in the numbers. An expired contract closes at zero: the whole premium is the loss (or,
  for one sold to open, the gain). Assignment and exercise close the option at zero too; the
  shares that change hands appear as their own buy or sell.
- **The money-market sweep** (SPAXX) moves cash in and out every day; those purchases and
  redemptions are cash management, not trades, and are left out.
- **History starts at the first synced transaction.** A sale of something bought before then
  has an unknown cost: it's marked "cost unknown" and left out of realized P&L rather than
  counted wrong.
- Fidelity reports tax-lot (FIFO) gains; this uses average cost, so a partial sale's realized
  figure can differ from Fidelity's. The total is the same once a position is fully closed.

Options are shown the way the broker shows them: contracts and premium per share (the stored
price is per contract), with the underlying's price, how far in or out of the money, breakeven
(strike plus or minus the premium paid) and days to expiry. They sit in their own asset class,
in their underlying's sector.

## Two kinds of performance

| | Account performance | Current-holdings backtest |
|---|---|---|
| Question | How did my account actually do? | How would today's mix have behaved? |
| Method | Daily time-weighted return from snapshots, with deposits/withdrawals removed | Today's weights held constant over past prices |
| History | Starts at the first sync | Available immediately (as far back as bars go) |
| Caveat | Needs some weeks of syncs to mean anything | Ignores every trade you made; describes the mix, not your track record |

Both are compared with **S&P 500 (SPY)**, **Nasdaq 100 (QQQ)** and a **60/40 SPY/IEF** mix on the
same tear-sheet metrics (CAGR, volatility, Sharpe, max drawdown, beta to SPY).

Money-market sweep positions (Fidelity's SPAXX core position) are flagged by SnapTrade as cash
equivalents and are already inside the account's cash balance, so they are not stored as
holdings; holdings + cash reconcile with the broker's reported total (checked against a live
Fidelity account to the cent).

## Classification

Sectors and asset classes come from `config/classifications.toml` (GICS sector for single stocks,
asset class for funds). Anything missing shows as "Unclassified". For your own holdings, add them
to `config/classifications.local.toml` instead: same format, git-ignored, and it wins over the
shared file, so your positions never end up in the public repo.

## Known gaps

- SnapTrade's daily plans cache positions once a day. Quantities rarely change intraday; live
  values come from the dashboard's quote stream.
- Options and crypto positions are stored and valued but not yet analysed.
