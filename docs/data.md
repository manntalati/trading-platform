# Market data

How daily bars get from Alpaca into research code, and the rules they're held to. Commands assume
you're in the repo root with `.env` filled in (see `.env.example`).

## Quick reference

```bash
uv run tp-data check                       # keys, connectivity, clock
uv run tp-data bars backfill --years 5     # one-off: 5y of bars + corporate actions
uv run tp-data bars daily                  # scheduled: latest sessions (+ new symbols)
uv run tp-data bars rebuild                # re-derive clean/ from raw/ (no network)
uv run tp-data bars backfill --source fake # the whole pipeline on synthetic data, no keys
uv run tp-data options snapshot            # today's chains: [options] universe + holdings
uv run tp-data macro backfill               # one-off: VIX and Treasury yields from FRED since 1990
uv run tp-data macro daily                  # scheduled: the last 30 days
uv run tp-data fundamentals                 # SEC filings: company facts and earnings dates
uv run tp-data bars intraday --symbols SPY,QQQ --days 250  # 5-minute bars for intraday research
```

```python
from tp_core.bars import close_matrix, load_bars
from tp_core.storage import Lake

bars = load_bars(Lake(Path("data")), ["SPY", "QQQ"], start=date(2020, 1, 1))
closes = close_matrix(bars)  # adjusted closes, one column per symbol
```

## Lake layout

```
data/
  raw/                                  immutable: written once per run, never modified
    alpaca/stock_bars_1d/ingest_date=YYYY-MM-DD/run-<run id>.parquet
    alpaca/corporate_actions/ingest_date=YYYY-MM-DD/run-<run id>.parquet
    alpaca/option_chain_snapshots/snapshot_date=YYYY-MM-DD/underlying=<SYM>/run-<run id>.parquet
    fred/macro_1d/ingest_date=YYYY-MM-DD/run-<run id>.parquet
    alpaca/stock_bars_5m/ingest_date=YYYY-MM-DD/run-<run id>.parquet
    sec/company_facts/ingest_date=YYYY-MM-DD/run-<run id>.parquet
    sec/earnings_events/ingest_date=YYYY-MM-DD/run-<run id>.parquet
  clean/                                derived: rebuilt from raw/ on every run
    stock_bars_1d/symbol=<SYM>/part.parquet
    stock_bars_1d_quarantine/part.parquet
    macro_1d/part.parquet
    stock_bars_5m/symbol=<SYM>/part.parquet
    sec_facts/part.parquet
    earnings_events/part.parquet
  reports/validation/stock_bars_1d-<UTC time>.json
  reports/options_snapshot/<date>-<run id>.json
  reports/macro/<date>.json
  reports/fundamentals/<date>.json
```

- **Raw is append-only.** Each run writes new files stamped with `ingested_at` and `run_id`.
  When the same `(symbol, session)` appears in several runs, the clean build uses the latest
  run, so vendor corrections flow through while the history of what we were told is kept.
- **Clean is disposable.** `tp-data bars rebuild` regenerates it from raw at any time, so a bug
  in cleaning or adjustment is fixed by fixing the code and rebuilding, never by editing data.
- Raw bars are requested **unadjusted** (`adjustment=raw`); adjustment is done by us (below).
- Schemas are in `libs/core/src/tp_core/schemas.py` and are enforced on every write.
- `data/` is git-ignored. Back it up separately (later: MinIO/S3).

## Clean bars columns

| Column | Meaning |
|---|---|
| `symbol`, `session` | ticker, NYSE trading date |
| `timestamp` | vendor bar time (UTC); for Alpaca daily bars, midnight New York |
| `open` `high` `low` `close` `volume` | as traded (use for order prices, sizes, liquidity) |
| `trade_count`, `vwap` | from the vendor |
| `split_factor`, `div_factor` | backward adjustment factors (1.0 on the latest bar) |
| `adj_open` … `adj_close` | `raw × split_factor × div_factor` (use for returns and signals) |
| `adj_volume` | `volume / split_factor` (share counts comparable across splits) |
| `feed`, `ingested_at`, `run_id` | lineage |

## Adjustment method

Backward, so the latest price equals the traded price, applied per symbol from the stored
corporate actions:

- **Splits** (forward/reverse, ratio `r = new_rate / old_rate`): earlier prices × `1/r`, volume × `r`.
- **Stock dividends** (`k` new shares per share): treated as a split with `r = 1 + k`.
- **Cash dividends** (`D` per share): earlier prices × `1 − D / close_prev`, `close_prev` being the
  raw close on the session before the ex-date. This makes adjusted returns total returns.
- **Point in time:** an action whose ex-date is after the last stored bar is not applied, and
  the corporate-actions fetch never looks past the last completed session.

Not handled yet (they are rare in the current universe but matter for a broad one): spin-offs,
mergers, rights issues, symbol changes, delistings.

## Validation

Every build runs the checks in `tp_core.validate` and writes a JSON report.

| Check | Severity | Action |
|---|---|---|
| `bad_timestamp`: not midnight New York, not an XNYS session, or in the future | error | quarantined |
| `duplicate`: same symbol/session twice within one run | error | quarantined |
| `bad_price`: an OHLC value missing, zero or negative | error | quarantined |
| `ohlc_inconsistent`: low/high don't bracket open/close | error | quarantined |
| `negative_volume` | error | quarantined |
| `zero_volume` | warning | kept |
| `missing_sessions`: NYSE sessions with no bar inside a symbol's history | warning | kept |
| `price_jump`: adjusted close-to-close move > 20% | warning | kept |
| `adjustment`: a corporate action that could not be applied | warning | kept |

A split missing from the corporate-actions feed shows up as a `price_jump` warning on the ex-date,
which is exactly the case the jump check exists for.

## When the daily job fails

1. `journalctl --user -u tp-bars-daily -n 200` for the log; the newest file in
   `data/reports/validation/` for the full list of issues.
2. Exit `2`: configuration (keys, universe file). Fix `.env` / `config/universes.toml`.
3. Exit `1`: some rows were quarantined. Inspect them:
   ```python
   pd.read_parquet("data/clean/stock_bars_1d_quarantine/part.parquet")
   ```
   Cross-check the bar elsewhere (broker chart, Massive/Polygon, Yahoo). Vendor glitches often
   get corrected; the next run re-fetches the last 5 sessions and supersedes the bad row.
4. Never hand-edit files under `raw/`. If a correction is needed, fix the code (or add an
   override mechanism) and `tp-data bars rebuild`.

## Option chain snapshots

Free historical options chains are scarce, so we build our own: every trading day at 15:45 ET
`tp-data options snapshot` stores the full chain (expirations up to `max_dte`: 800 days, so LEAPS
are included) for each underlying in the `[options]` section of `config/universes.toml`, your
`[research] symbols` in `config/universes.local.toml`, and everything you hold (stocks and the
underlyings of your options). The 30-day implied volatility of each snapshot is kept in
`clean/option_iv_daily` for the options research's IV rank ([research.md](research.md)).

Each row is one contract at one moment (`RAW_OPTION_CHAIN_SCHEMA`): OCC symbol plus parsed
`expiration`/`right`/`strike`/`dte`; the underlying's last trade and quote; the option's bid/ask
and sizes, last trade, implied volatility and greeks from the chain snapshot; and open interest
and the previous close from the contracts endpoint (both as of the prior day).

```python
from tp_core.chains import load_chain_snapshots

spy = load_chain_snapshots(Lake(Path("data")), "SPY")
```

Caveats:

- **Free plan = indicative feed.** Alpaca's free options data is an indicative (derived, delayed)
  feed, not OPRA. Good for IV levels, skew and term structure; don't treat quotes as fills.
  `TP_OPTIONS_FEED=opra` switches once on a paid plan.
- Snapshots are stored as-is, without a clean layer yet: a crossed quote (bid > ask) is counted
  in the job report, not removed.
- A missed day can't be backfilled from Alpaca later; that's the point of running it daily.

## Macro series (VIX and Treasury yields)

`tp-data macro daily` downloads five daily series and keeps them like the bars: one immutable raw
file per run, `clean/macro_1d` rebuilt with the latest download winning per series and day
(`tp_core.macro`). They're stored under FRED's names, whichever source served them:

| Source | Series | Needs |
|---|---|---|
| FRED's API | all five | a free key in `FRED_API_KEY` ([get one](https://fred.stlouisfed.org/docs/api/api_key.html)); tried first when set |
| Cboe's history files | `VIXCLS`, `VXVCLS` | nothing |
| The Treasury's daily par yield curve | `DGS3MO`, `DGS2`, `DGS10` | nothing |
| FRED's graph download | all five | nothing; often hangs for non-browser clients, so it is the last resort |

Cboe and the Treasury publish these numbers and FRED republishes them, so the values match.
A host that times out or refuses a connection is skipped for the rest of the run. The summary
says which source served each series; `--source fred-api|publishers|fred-csv` forces one.

| Series | What | Used for |
|---|---|---|
| `VIXCLS` | VIX: the S&P 500's 30-day implied volatility | the volatility regime: calm under 15, stressed over 25 |
| `VXVCLS` | the 3-month VIX | VIX / VIX 3-month above 1 (an inverted volatility curve) marks stress |
| `DGS3MO` | 3-month Treasury yield | the risk-free rate in option pricing (falls back to `TP_RISK_FREE_RATE` when missing or more than ten days old) |
| `DGS2`, `DGS10` | 2- and 10-year Treasury yields | the rate regime (10-year rising, steady or falling against its 200-day average, a quarter point either way) and the curve (10-year minus 3-month: inverted under 0, flat under half a point) |

```python
from tp_core.macro import load_macro
from tp_core.regime import market_regimes

macro = load_macro(Lake(Path("data")))  # one column per series, one row per day
regimes = market_regimes(closes, macro=macro)  # trend, volatility (VIX), breadth, rates, curve
```

Each value is the one for that day's close, so a strategy acting at the next open could have
known it; regime breakdowns attribute each day's return to the labels of the day before anyway.
FRED skips some days the exchange trades (and vice versa); values carry forward up to five days.

## Fundamentals (SEC EDGAR)

`tp-data fundamentals` reads two free documents per company from SEC EDGAR for the bars
universe, your research list and your holdings (funds have none): the **company facts** (every
figure tagged in its 10-Q and 10-K filings) and the **submissions** (its list of filings). The
SEC asks every client to say who it is: set `TP_SEC_USER_AGENT` to a name and an email address
in `.env`. Requests are paced under EDGAR's limit of ten a second.

| Kept | From | Used for |
|---|---|---|
| Diluted EPS, net income, revenue, gross profit | each quarter (the fourth is the year less three quarters) | trailing twelve months, P/E, earnings surprises |
| Stockholders' equity, total assets, shares outstanding | each reporting date | book to market, gross profitability, market cap |
| Earnings releases (8-K, item 2.02) | the filings list, with the acceptance time | when results became public |

**Point in time.** Every figure carries the date it was filed, and a figure only counts from
then (`tp_core.fundamentals`, `as_of=`): a backtest can't see a quarter's results before they
were published. A later filing that restates a quarter counts from its own date; the first
publication date is kept as `known`. Results are public at the earnings release when there is
one (a release after the close counts for the next session), else at the 10-Q or 10-K.

```python
from tp_core.fundamentals import load_facts, quarterly, snapshot, sue

facts = load_facts(Lake(Path("data")))
eps = quarterly(facts, "XYZ", "eps_diluted", as_of=date(2024, 6, 30))
surprises = sue(eps)  # Bernard-Thomas standardized unexpected earnings
known = snapshot(facts, ["XYZ", "ABC"], date(2024, 6, 30))  # trailing EPS, equity, assets...
```

EPS is per share as reported: compare it across a split with `split_adjusted_eps` (scales each
quarter by the split factor of the day it was filed). EDGAR has XBRL figures from 2009 on; tags
differ between companies and years (revenue has four common ones), so a company can lack a
concept for some quarters.

## Intraday bars

`tp-data bars intraday` downloads 5-minute bars (`--minutes` for another length) for a few
symbols over the last `--days` sessions, regular hours only (9:30 to 16:00 New York), into
`clean/stock_bars_5m` (`tp_core.intraday.load_intraday`). Prices are raw: fine within a day,
which is what intraday research needs. Run it by hand when you want more history; downloading an
overlapping window again replaces bars rather than duplicating them. Alpaca's intraday history
goes back to 2016.

## Known limitations

- **Survivorship bias.** `config/universes.toml` lists today's large caps. Backtests on it
  overstate returns; don't make cross-sectional claims from it.
- **Free-tier feeds.** Historical SIP is free only for data older than 15 minutes; the daily
  job runs at 18:30 ET so this is never an issue.
- **Alpaca history starts in 2016.** Longer studies (e.g. Faber's 10-month MA) need another
  source for the early years; see the notebooks.
