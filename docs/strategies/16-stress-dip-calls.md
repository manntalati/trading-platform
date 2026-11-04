# Strategy 16: stress-dip calls, 1-3 session swing calls on the index funds

- Family: short-term reversal, options swing (1-3 sessions)
- Sources: Connors and Alvarez's RSI(2) pullbacks (strategy 5 here) for the trigger; Nagel,
  "Evaporating Liquidity" (Review of Financial Studies, 2012), for why reversals pay more when
  the VIX is high; the internal bar strength (IBS) close-near-the-low effect as it's commonly
  traded on index funds. Citations from general knowledge; check the details against the
  sources. Designed by Rex (Markets Analyst), 2026-11-02.
- Code: [`library/stress_dip.py`](../../strategies/src/tp_strategies/library/stress_dip.py)
  (`tp-backtest research stress-dip-calls`), the playbook's `stress_dip` setup (Research → Ideas
  → Stress-dip calls)
- Stage: 1 (research backtest, and ideas tracked in the track record). Not on paper: see
  [Before trading it](#before-trading-it).

## Economic rationale

Short-term reversal is pay for supplying liquidity. Sellers who need out on a bad afternoon push
an index fund below where patient buyers step in, and the price snaps back within days. Nagel
found reversal returns much larger when the VIX is high: liquidity is scarcer, so supplying it
pays more. An index fund's dip is mostly flows; a single stock's is often news that keeps going
(earnings, downgrades), which is why the rule stays on the funds. The bounce is mostly overnight,
so the entry has to be in the signal session's last minutes, and a call 14-30 days out at 0.70
delta carries the move with leverage while the 200-day filter keeps it out of bear markets.

## Rules

| | |
|---|---|
| Universe (tier 1, tradeable) | QQQ, SPY, IWM; DIA optional (wider spreads) |
| Universe (tier 2, ideas and research only, tracked apart) | Sector funds XLF, XLK, SMH, XLE, XLV, GLD; mega caps NVDA, AAPL, AMZN, MSFT, GOOGL, META, AVGO, AMD, TSLA, JPM only with the previous VIX close at 25+ and no results within 5 sessions. Single stocks failed on real option prices: don't trade tier 2 |
| Trend | The close above its 200-day average (and the 50-day above the 200-day for the RSI branch) |
| Trigger | RSI(2) under 10, or a down day with IBS = (close - low) / (high - low) under 0.2 |
| Stress | The previous session's VIX close above the 10-day average of VIX closes ending then, or at least 20. Only the previous session: known at 15:45. A VIX older than the previous session counts as unknown (no signal) |
| When | Worked out 15 minutes before the close (12:45 on a half day) on the day's bar so far: the last trade as the close, the day's high and low so far |
| Contract | A call 14-30 days out (the expiry nearest 21), delta 0.65-0.75 (nearest 0.70), spread at most 3% of the mid, open interest 500+. A fixed rule, not the playbook's horizon optimizer (whose 21-days-left-at-the-horizon constraint doesn't fit a 3-day hold); its numbers are shown for information |
| Entry | A limit at the mid; 8 minutes before the close (15:52) raised to the mid plus a quarter of the spread, never above the ask; not filled 2 minutes before the close (15:58): cancelled. No entry the next day |
| Exits | Checked about 15:45 each session, sold before the close: the fund closes above its 5-day average; the option is down 40%; or the third session. No profit target |
| Concurrency | One new entry a day (the lowest RSI(2)); one per fund; at most 2 open |
| Events | FOMC, CPI or jobs day inside the hold: untested, a warning at most. Tier 2 stocks: results within 5 sessions blocks |
| Sizing | Risk = half the premium (a gap can blow through the 40% stop), within `TP_OPTIONS_RISK` (2%): about a $55k account for one IWM contract, $150k+ for one QQQ contract. The ideas size to it and say when one contract is over |

## How it runs live

1. **15:45** (`options-late` on the scheduler, 15 minutes before the close on the exchange
   calendar): `tp-data options late` snapshots the `[options] late` names in
   `config/universes.toml` (tier 1 and tier 2), and with them Alpaca's stock snapshot for each:
   the last trade and the session so far (`dailyBar`), stored under `raw/alpaca/stock_snapshots`
   (`tp_core.intraday.provisional_bars`). The free plan's quotes feed is IEX: its high and low
   were within a few cents of the consolidated bar on the days checked, and its last trade is
   real time. Every chain snapshot now keeps these too.
2. The scheduler makes the recommendations as soon as that succeeds (not at its next tick or
   after the usual 10-minute gap), so the idea is on Research → Ideas → Stress-dip calls, and in
   Discord with `TP_DISCORD_WEBHOOK_URL`, about a minute after 15:45. The regular closing
   snapshot starts later (about 15:52, after the grace it gives cron) and finishes too late for
   this entry, which is why the late snapshot exists.
3. The idea says which fund is the day's entry, the contract, the limit, the 15:52 reprice and
   the 15:58 cancel, the stop price, the sell-by session and today's 5-day average.
4. The list is empty outside the last 20 minutes before the close: the rule is entered on the
   signal session or not at all. The idea stays in the track record, which assumes the 15:52
   limit filled and follows its exit plan through the daily snapshots (Research → Track record,
   `tp-recs scorecard`), split by setup: `stress_dip` (tier 1) and `stress_dip_research` (tier 2).

`tp-recs calibrate` doesn't calibrate these setups: its events are bar closes without the VIX or
a 15:45 bar, and its exit grid would replace the fixed exits (adding profit targets the study
ruled out). The study below is their calibration.

## How to run

```bash
uv run tp-backtest research stress-dip-calls          # the study; kept in reports/research/
uv run tp-backtest research stress-dip-calls --json
uv run tp-data options late                           # what the scheduler runs at 15:45
```

The real-price check needs `tp-data options history`; the 15:45 check needs
`tp-data bars intraday --symbols QQQ,SPY,IWM,DIA`.

## What the study found (your data, run 2026-11-04)

Daily bars 2021-10 to 2026-11-03 (signals from 2022-07 after the 200-day warm-up); test: the
sessions from 2025-02-18, the later 40%. Returns on the premium per trade.

**The underlying, 3 sessions after the signal close (QQQ, SPY, IWM)**

| Signal | Train n | Train z | Test n | Test z | Test up | Test mean 3d | vs every day |
|---|---|---|---|---|---|---|---|
| Dip, stressed | 162 | +0.27 | 104 | +0.37 | 61% | +0.83% | +0.64% |
| Dip, any VIX | 296 | +0.19 | 198 | +0.13 | 54% | +0.42% | +0.23% |
| Dip, not stressed | 134 | +0.09 | 94 | -0.13 | 47% | -0.03% | -0.22% |
| Every day | | +0.04 | 1,284 | -0.03 | 57% | +0.19% | |

The stress filter is where the edge is: without it the dip is about nothing in the test half.

**Calls, modeled** (Black-Scholes at IV = each fund's 30-day IV over the VIX x the VIX; 21 days,
0.70 delta, the spec's exits)

| Cost a round trip | Train n | Train mean | Test n | Test mean | Test median | Test won |
|---|---|---|---|---|---|---|
| 1% | 162 | +7.0% | 104 | +7.8% | +13.0% | 61% |
| 2% | 162 | +6.0% | 104 | +6.8% | +12.0% | 61% |
| 5% | 162 | +3.0% | 104 | +3.8% | +9.0% | 60% |
| 2%, as traded (1 a day, 2 open) | 63 | +8.0% | 42 | +4.6% | +7.9% | 55% |
| 2%, every day (the model's control) | | | 1,284 | -0.7% | +1.6% | 54% |

- Expiry and delta: 14-30 days and 0.70-0.80 do best in both halves; 7 days has the highest mean
  but its worst tenth loses 60-70%; 45 days gives up about half. The model's control is about
  zero after costs here, so the model isn't flattering the signal.
- Exits chosen on the training signals were "3 sessions" alone (no stop, no 5-day exit) for
  nearly every expiry and delta: a higher test mean (+11.1% at 21 days, 0.70) with a median near
  zero. The spec's exits trade some mean for a +12% median and a 61% win rate. Kept as specced;
  on real prices the 5-day exit raised the win rate.
- Profit targets on daily closes change almost nothing (+20% cuts the mean slightly): the big
  up days close above the 5-day average, which exits them anyway.

**Calls, real prices** (`option_bars_1d` trade closes, Feb 2024 to Oct 2026; strikes 93-100% of
spot, 10-60 days; the spec's exits; one row per fund and session)

| Group, stressed dips | Period | n | Gross mean | After 2% | Won |
|---|---|---|---|---|---|
| Index funds (QQQ, SPY, IWM, DIA) | all | 28 | -3.4% | **-5.4%** | 50% |
| Index funds | test half | 19 | +6.4% | **+4.4%** | 63% |
| Every fund (index and sector) | all | 97 | -3.4% | -5.4% | 48% |
| Every fund | test half | 64 | +1.7% | -0.3% | 53% |
| Single stocks | test half | 328 | +0.4% | -1.6% | 48% |
| Index funds, every day (control) | test half | 214 | -2.1% | -4.1% | 51% |

**Negative after costs over the whole real-price period.** The sample is tiny (28 index-fund
signals; SPY has no contracts in it: the lake only has the contracts the playbook's backtest
happened to request), by expiry it's noisy (10-20 days -17%, 20-35 days +31% on 5 trades), and
the control is negative too (the contracts in the lake are a biased sample). It is the only
check on real prices, and it doesn't support the strategy yet.

The study that proposed this strategy found +5.0% on 140 fund signals. Re-running its
construction reproduces that (+4.2% on 133), but it read the **same day's VIX** for the stress
filter, which isn't known at 15:45, and exited on the contract's next three trades without the
5-day exit. With the previous day's VIX, as the rule requires, the same construction gives
+1.8% gross on 97 fund signals and -0.1% on 27 index-fund signals.

**At 15:45** (5-minute bars, 2022-11 to 2026-10; the rule on the 15:45 price and the day's range
so far; calls bought and sold at 15:45 prices, modeled)

| | Train n | Train after 2% | Test n | Test after 2% | Test won |
|---|---|---|---|---|---|
| Signal at 15:45 | 234 | +2.8% | 144 | +7.1% | 65% |
| Signal at the close | 226 | +1.9% | 136 | +6.1% | 62% |
| Every day (control) | 2,276 | +1.0% | 1,704 | -0.2% | 52% |
| 15:45, as traded, test, 1% / 2% / 5% | | | 52 | +7.7% / +6.7% / +3.7% | 67% |

319 signals at both, 59 only at 15:45, 43 only at the close (about 1 in 6 signals at 15:45 isn't
there at the close). The 15:45 version earns at least as much as the close version: entering on
the provisional bar doesn't cost the edge, in the model.

## Before trading it

- **The real-price check is negative after costs over its whole period.** Until a larger sample
  of real (or quoted) option prices says otherwise, this is a research and ideas strategy. The
  paper sleeve (long calls on Alpaca paper; the account is approved for options level 3) is held
  until that's resolved.
- **Spreads.** The lake's chain quotes are Alpaca's indicative feed, wider than the real market;
  2% a round trip is about right for QQQ/SPY/IWM at 0.70 delta, much more for sector funds.
- **Thin edge.** About +3-7% a trade after costs in the model, a win rate in the mid-50s to
  60s, frequent 30-40% losers, about 30-40 signal days a year. Thirty trades take 9-12 months.
- **Ex-dividend days.** The provisional bar is raw and the history is adjusted; on a fund's
  ex-dividend day the dividend shows as part of the day's drop (0.3-0.6% for these funds).
- **Fidelity is read-only.** The ideas are for you to act on, or not.

## Known failure regimes

- **Dips that keep dipping** (a 2022-style bear): the 200-day filter should keep it out; watch for
  several 3-session stops in a row.
- **The calls lose while the fund wins** (the fund up after 3 sessions 55%+ of the time but the
  calls' mean below zero): vega and theta are eating it; move to 0.80 delta or a call debit
  spread.
- **Event days inside the hold** (FOMC, CPI, payrolls): untested.
- **Volatility collapsing after the bounce**: the model ties each fund's IV to the VIX; a sharper
  IV drop than the VIX's costs more than modeled.

## Promotion notes

Invalidated if, after 30+ tracked ideas or paper trades, the mean after costs is at or below
zero or the win rate under 45%. Promotion to paper needs the real-price question answered first:
more contracts in `option_bars_1d` for SPY, QQQ and IWM at 14-30 days (a targeted
`tp-data options history` request), or a few months of the track record following real quotes.
The paper sleeve then needs long-call support in `tp_risk` (whitelisted strategies, premium
caps), an end-of-day bot task for the 15:45-15:58 entry and exits, option fills with the x100
multiplier, and option reconciliation, with `[strategies.stress-dip-calls]` in
`config/paper.toml` once you choose where its capital comes from.
