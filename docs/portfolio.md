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
5. Enable the daily timer (`tp-broker-sync`, weekdays 17:45 ET; see [infra/](../infra/README.md)).

No keys yet? `uv run tp-broker sync --source fake` loads a Fidelity-shaped demo portfolio, and
`--source alpaca` shows your Alpaca paper account.

## What gets stored

Under `data/raw/broker/` (git-ignored, never pushed):

| Dataset | One row per | Notes |
|---|---|---|
| `accounts` | account per sync | cash, broker-reported total; account number masked to last 4 digits |
| `holdings` | position per sync | quantity, broker price, market value, average cost per unit, kind, underlying; options are stored per contract (price and cost x100) |
| `activities` | transaction | buys, sells, dividends, contributions, withdrawals, fees; re-fetched with a 30-day overlap, de-duplicated by id |

Every market-priced holding (stocks, ETFs) is added to the daily bars job automatically, so
holdings outside `config/universes.toml` get price history too. Mutual funds (e.g. FXAIX) are not
on Alpaca's market data, so they are valued at the broker's price but left out of the
price-based analytics (the dashboard shows how much of the portfolio that covers).

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
