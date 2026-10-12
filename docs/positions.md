# Positions: statistics and Claude

The **Positions** tab shows every open position with the numbers you need to decide what to do
with it, and lets you ask Claude about any of them. It's read-only: nothing here can trade.

## What each position shows

**Stocks and funds** (from the daily bars in the lake, priced live while the market is open):
today's change, returns over 1 week to 1 year and this year, the 52-week range and how far below
the high, the 20-, 50- and 200-day averages, 20- and 60-day volatility, beta to the S&P 500, the
worst drop over the year, P&L against your average cost, and the weight in the portfolio. Mutual
funds have no market data, so they show the broker's price and P&L only.

**Options** (the per-contract figures brokers quote, plus):

| | What it tells you |
|---|---|
| **Bid / ask, mark** | the live market for the contract (Alpaca, indicative feed on the free plan); the mark is the mid. Without a quote, the broker's last price, flagged |
| **Sell now** | what closing at the bid gets, for the whole position |
| **Breakeven, needs** | the underlying's price at expiry where the position breaks even, and the move from here |
| **Time decay** | dollars a day the position loses from time alone (theta), and how much of the price is time value |
| **Odds** | chance of finishing in the money and at a profit, as the option's price implies |
| **Market-implied move** | one standard deviation of the underlying by expiry, from the implied volatility |
| **Implied volatility** | solved from the mark, next to the stock's own 20-day volatility |
| **Delta, gamma, vega** | how many shares the position behaves like, how fast that changes, and dollars per point of volatility |
| **Scenarios** | position P&L if the underlying moves −10% to +10%, now, midway and at expiry |
| **Exposure** | per underlying, shares plus what the options behave like, so stacked bets on one name show |

The maths is Black-Scholes (`tp_core.greeks`): close enough for reading a position's risk, not
for making a market. US equity options are American; for calls on stocks without a dividend
before expiry, and near expiry, the difference is cents. Time runs to 4:00 PM New York on the
expiration day. The risk-free rate is `TP_RISK_FREE_RATE` (default 4%).

The odds are **risk-neutral**: what the market is charging for, not a forecast. That's the right
yardstick for "hold or sell": at today's implied volatility, holding and selling have about the
same expected value, so holding pays only if you expect a bigger (or earlier) move than the
market does. The scenarios hold implied volatility constant; after earnings or news it usually
falls, which hurts a long option even when the stock moves your way.

## Notes

Each position gets a short list of plain-language notes (`tp_core.positions`), for example:

- **Expires today / Exercise at expiry**: an option in the money at the close is exercised
  automatically. For a long call that means buying 100 shares per contract at the strike; the
  note says how much cash that takes. Sell before the close if you don't want that.
- **Out of the money** with a week or less left: what the underlying has to do by when.
- **Time decay**, **What it needs**, **Hold or sell**, **The loss so far**: the trade-off in
  numbers. A loss already taken is spent either way; the choice is between what the contract
  fetches now and the range in the scenarios.
- **Implied vs actual volatility**: options pricing much bigger moves than the stock has been
  making, which often happens ahead of earnings.
- **Wide market**: a spread over 10% of the price, and what selling at the bid gives up.
- **Price below exercise value**: a stale price (the broker's, usually).

These lay out the trade-offs; they aren't advice.

## Ask Claude

Each position (and the account as a whole) has a chat with Claude, which gets that position's
figures, the notes, and a summary of every other position. It can search the web for earnings
dates and news before expiry. Ask follow-ups; the conversation stays on the page until you
reload it.

Set it up once:

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...      # from the Claude Console (https://platform.claude.com), API keys
```

Then restart `tp-api`. Options (all optional):

| Variable | Default | |
|---|---|---|
| `TP_CLAUDE_MODEL` | `claude-opus-5-5` | the model |
| `TP_CLAUDE_EFFORT` | `medium` | `low` to `max`: how hard it thinks; higher is slower and costs more |
| `TP_CLAUDE_WEB_SEARCH` | `true` | let it search the web; each search adds a small charge |

What to know:

- **Your data goes to Anthropic.** Each question sends the position's figures, your portfolio
  summary (symbols, quantities, values, P&L; no account numbers) and the conversation to
  Anthropic's API. Leave the key unset to keep everything local.
- **It's billed to your API account**, per question. The page shows the tokens each answer used.
- **Declined requests fall back.** If the model declines a request, the API retries it on
  another model Anthropic picks (server-side fallback), instead of returning nothing.
- The request is guarded like the other writes (dashboard header, same origin), so another web
  page can't run up your bill.

## API

| Endpoint | What |
|---|---|
| `GET /api/positions` | every position with `stats` and `notes`, totals (P&L, time decay), exposure by underlying, cash reconciled with the broker's total, and where quotes came from |
| `POST /api/positions/ask` | `{"question": "...", "symbol": "XYZ..."?, "history": [{"role": "user"\|"assistant", "content": "..."}]}` → `{"answer", "sources", "usage", ...}` |

Option quotes are fetched when the page asks and reused for 30 seconds. They need `tp-api`
started with live quotes (`--quotes alpaca`, the default) and Alpaca keys; in the demo modes
the page uses the broker's prices.
