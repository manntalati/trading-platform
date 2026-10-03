# ADR 0002: local Parquet lake with immutable raw zone and explicit adjustment

- Status: accepted
- Date: 2026-10-03

## Context

Phase 0 needs 5 years of daily bars for ~50 symbols, refreshed daily, and the plan's data rules
require raw data stored immutably, explicit split/dividend adjustment with both series kept,
point-in-time correctness, and validation on every load. Volume is tiny (~60k rows), there is
one machine, and the storage must later move to object storage (MinIO/S3) without code changes
beyond the root path.

## Decision

- **Parquet files on the local filesystem**, behind a `Lake(root)` object; later the root becomes
  an object-store path.
- **Raw zone** partitioned by `ingest_date`, one immutable file per run (`run-<id>.parquet`,
  written atomically, refusing to overwrite). Each row carries `ingested_at` and `run_id`.
- **Clean zone** partitioned by `symbol`, fully derived from raw by a deterministic rebuild that
  takes the latest run per `(symbol, session)`.
- **Bars are fetched unadjusted** and adjusted by our own code from stored corporate actions,
  keeping raw and adjusted columns side by side.
- **pandas + pyarrow** for the data layer (what the SDKs, calendars and backtest libraries speak);
  Arrow schemas enforced on every write. DuckDB can query the same files later.
- Vendor access sits behind small `Protocol`s (`BarsSource`, `CorporateActionsSource`) with a
  deterministic `FakeSource`, so jobs and tests never need the network.

## Consequences

- Any cleaning or adjustment bug is fixed by changing code and running `tp-data bars rebuild`.
- Raw storage grows with every daily run (re-fetching a 5-session overlap); at this scale that's
  kilobytes per day. Compaction can come with the move to object storage.
- Full rebuilds read all of raw: trivial now, revisit when intraday data arrives (partition the
  rebuild by symbol or by date).
- Adjustment covers splits, stock dividends and cash dividends; spin-offs, mergers and symbol
  changes are not handled yet.
