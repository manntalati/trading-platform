# Strategy 16: stress-dip calls, swing calls held up to 5 sessions on the index funds

- Family: short-term reversal, options swing (up to 5 sessions)
- Sources: Connors and Alvarez's RSI(2) pullbacks (strategy 5 here) for the trigger; Nagel,
  "Evaporating Liquidity" (Review of Financial Studies, 2012), for why reversals pay more when
  the VIX is high; the internal bar strength (IBS) close-near-the-low effect as it's commonly
  traded on index funds. Citations from general knowledge; check the details against the
  sources. Designed by Rex (Markets Analyst), 2026-11-02.
- Code: [`library/stress_dip.py`](../../strategies/src/tp_strategies/library/stress_dip.py)
  (`tp-backtest research stress-dip-calls`), the playbook's `stress_dip` setup (Research → Ideas
  → Stress-dip calls); the exits and the VIX timing in
  [`config/stress_dip.toml`](../../config/stress_dip.toml) (see [Settings](#settings))
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
| Universe (tier 2, ideas and research only, tracked apart) | Sector funds XLF, XLK, SMH, XLE, XLV, GLD; mega caps NVDA, AAPL, AMZN, MSFT, GOOGL, META, AVGO, AMD, TSLA, JPM only with the VIX the stress filter read at 25+ and no results within 5 sessions. Single stocks failed on real option prices: don't trade tier 2 |
| Trend | The close above its 200-day average (and the 50-day above the 200-day for the RSI branch) |
| Trigger | RSI(2) under 10, or a down day with IBS = (close - low) / (high - low) under 0.2 |
| Stress | The VIX above its 10-day average, or at least 20. By default (`vix_timing = "live"`) the VIX at about 15:45 from Cboe's delayed quote (15 minutes late, so about 15:30), against the 10-day average with it as the last close; when the quote can't be had (unreachable, not today's, or more than 30 minutes old) the previous close stands in and the idea says so. `vix_timing = "prev"` always reads the previous session's close. A VIX history whose last close is older than the previous session counts as unknown (no signal) |
| When | Worked out 15 minutes before the close (12:45 on a half day) on the day's bar so far: the last trade as the close, the day's high and low so far |
| Contract | A call 14-30 days out (the expiry nearest 21), delta 0.65-0.75 (nearest 0.70), spread at most 3% of the mid, open interest 500+. A fixed rule, not the playbook's horizon optimizer (whose 21-days-left-at-the-horizon constraint doesn't fit a hold of a few sessions); its numbers are shown for information |
| Entry | A limit at the mid; 8 minutes before the close (15:52) raised to the mid plus a quarter of the spread, never above the ask; not filled 2 minutes before the close (15:58): cancelled. No entry the next day |
| Exits | Checked about 15:45 each session, sold before the close: the option is up 20% (take profit); down 40% (stop); or the fifth session after entry. Approved by the user 2026-11-04. Off by default, on in the settings: a floor (once up `floor_trigger_pct`, sell if it falls back to `floor_pct`, +10%) and the first spec's exit on a close above the 5-day average |
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
   the 15:58 cancel, the target and stop prices, the sell-by session, and which VIX the stress
   filter read (the live quote and its time, or the previous close, and whether that was a
   fallback; `vix_source`, `vix_at` and `vix_fallback` on the idea's `play`).
4. The list is empty outside the last 20 minutes before the close: the rule is entered on the
   signal session or not at all. The idea stays in the track record, which assumes the 15:52
   limit filled and follows its exit plan through the daily snapshots (Research → Track record,
   `tp-recs scorecard`), split by setup: `stress_dip` (tier 1) and `stress_dip_research` (tier 2).

`tp-recs calibrate` doesn't calibrate these setups: its events are bar closes without the VIX or
a 15:45 bar, and its exit grid would replace the approved exits. The study below and the
settings are their calibration.

## Settings

`config/stress_dip.toml` (`TP_STRESS_DIP_FILE` to point elsewhere; a git-ignored
`config/stress_dip.local.toml` next to it overrides any key). Every key is optional; `false`
turns an exit off. Read by the ideas (`tp-recs run`, the dashboard's Research) and by the study.

| Key | Default | |
|---|---|---|
| `take_profit_pct` | `0.20` | sell when the mark is up this much on the premium paid |
| `stop_pct` | `-0.40` | sell when it is down this much |
| `max_sessions` | `5` | sell on this session after entry at the latest (1-10) |
| `floor_trigger_pct` | `false` | once the mark has been up this much ... |
| `floor_pct` | `0.10` | ... sell if a later mark is back to this |
| `use_sma5_exit` | `false` | sell on the first close above the 5-day average |
| `vix_timing` | `"live"` | `"live"`: the ~15:45 VIX (fallback: the previous close); `"prev"`: the previous close |

An unknown key or a value out of range stops the run with the reason. The track record follows
the floor too when it's on (one mark a session, from the daily snapshots).

## How to run

```bash
uv run tp-backtest research stress-dip-calls          # the study; kept in reports/research/
uv run tp-backtest research stress-dip-calls --json
uv run tp-data options late                           # what the scheduler runs at 15:45
```

The real-price check needs `tp-data options history`; the 15:45 check needs
`tp-data bars intraday --symbols QQQ,SPY,IWM,DIA`.

## The approved defaults on real prices (your data, run 2026-11-05)

`option_bars_1d` trade closes, Feb 2024 to Oct 2026, the same contracts as below; +20% target,
-40% stop, by session 5; after 2% a round trip; one row per fund and session. In the study
"live" uses the session's VIX close in place of the 15:45 value (the lake has no intraday VIX).

| Index funds (QQQ, SPY, IWM, DIA) | VIX timing | n | After 2% | Won | Test half n | Test after 2% |
|---|---|---|---|---|---|---|
| Stressed dips | live (default) | 45 | **-0.4%** | 56% | 33 | **+3.2%** |
| Stressed dips | prev (t-1) | 28 | **-4.0%** | 54% | 19 | **-0.4%** |
| Every day (control) | | 301 | -2.5% | 51% | 194 | -5.7% |
| Every fund, stressed dips | live | 122 | -1.1% | | 86 | +0.6% |
| Every fund, stressed dips | prev | 95 | -4.4% | | 65 | -2.9% |

With the first spec's exits (SMA5 / -40% / 3 sessions) the same rows are -3.2% (n=46) and +2.8%
(n=34) with the live VIX, -5.4% (n=28) and +4.4% (n=19) with t-1. The approved exits help over
the whole period under both timings, and the live VIX beats t-1, but nothing is positive after
costs over the whole period: still paper only. Rex's +1.9% a trade (n=140) came from a different
construction (all funds, intraday highs for the target), not these rows.

## What the study found with the first spec's exits (your data, run 2026-11-04)

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
  several stops in a row.
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
