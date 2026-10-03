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
  clean/                                derived: rebuilt from raw/ on every run
    stock_bars_1d/symbol=<SYM>/part.parquet
    stock_bars_1d_quarantine/part.parquet
  reports/validation/stock_bars_1d-<UTC time>.json
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

## Known limitations

- **Survivorship bias.** `config/universes.toml` lists today's large caps. Backtests on it
  overstate returns; don't make cross-sectional claims from it.
- **Free-tier feeds.** Historical SIP is free only for data older than 15 minutes; the daily
  job runs at 18:30 ET so this is never an issue.
- **Alpaca history starts in 2016.** Longer studies (e.g. Faber's 10-month MA) need another
  source for the early years; see the notebooks.
