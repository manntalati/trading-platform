"""Dataset names and Arrow schemas for everything stored in the lake.

Schemas are enforced on write, so a vendor changing a field type fails loudly at ingest instead
of silently corrupting downstream tables.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa

# Dataset names (paths relative to raw/ or clean/).
RAW_STOCK_BARS_1D = "alpaca/stock_bars_1d"
RAW_CORPORATE_ACTIONS = "alpaca/corporate_actions"
CLEAN_STOCK_BARS_1D = "stock_bars_1d"
QUARANTINE_STOCK_BARS_1D = "stock_bars_1d_quarantine"
RAW_OPTION_CHAIN_SNAPSHOTS = "alpaca/option_chain_snapshots"
RAW_BROKER_ACCOUNTS = "broker/accounts"
RAW_BROKER_HOLDINGS = "broker/holdings"
RAW_BROKER_ACTIVITIES = "broker/activities"

_TS = pa.timestamp("ns", tz="UTC")

RAW_BARS_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("timestamp", _TS),  # vendor bar timestamp (Alpaca: midnight New York, in UTC)
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
        ("trade_count", pa.float64()),
        ("vwap", pa.float64()),
        ("feed", pa.string()),
        ("adjustment", pa.string()),  # always "raw": adjustments are applied by us, explicitly
        ("ingested_at", _TS),
        ("run_id", pa.string()),
    ]
)

# Split-like actions carry new_rate/old_rate (ratio = new/old shares); stock dividends carry
# `rate` as extra shares per share; cash dividends carry `rate` as cash per share.
CORPORATE_ACTION_TYPES = ("forward_split", "reverse_split", "stock_dividend", "cash_dividend")

RAW_CORPORATE_ACTIONS_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("symbol", pa.string()),
        ("action_type", pa.string()),
        ("ex_date", pa.date32()),
        ("process_date", pa.date32()),
        ("record_date", pa.date32()),
        ("payable_date", pa.date32()),
        ("new_rate", pa.float64()),
        ("old_rate", pa.float64()),
        ("rate", pa.float64()),
        ("special", pa.bool_()),
        ("foreign", pa.bool_()),
        ("ingested_at", _TS),
        ("run_id", pa.string()),
    ]
)

CLEAN_BARS_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("session", pa.date32()),
        ("timestamp", _TS),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
        ("trade_count", pa.float64()),
        ("vwap", pa.float64()),
        # adjusted = raw * split_factor * div_factor (backward adjustment: latest bar unchanged)
        ("split_factor", pa.float64()),
        ("div_factor", pa.float64()),
        ("adj_open", pa.float64()),
        ("adj_high", pa.float64()),
        ("adj_low", pa.float64()),
        ("adj_close", pa.float64()),
        ("adj_volume", pa.float64()),
        ("feed", pa.string()),
        ("ingested_at", _TS),
        ("run_id", pa.string()),
    ]
)


# One row per option contract per snapshot. Quote/trade/greeks come from the chain snapshot;
# open interest and the previous close from the contracts endpoint (both as of the prior day).
RAW_OPTION_CHAIN_SCHEMA = pa.schema(
    [
        ("snapshot_at", _TS),
        ("underlying", pa.string()),
        ("contract", pa.string()),  # OCC symbol
        ("expiration", pa.date32()),
        ("right", pa.string()),  # "C" or "P"
        ("strike", pa.float64()),
        ("dte", pa.int32()),  # calendar days from snapshot date to expiration
        ("underlying_price", pa.float64()),  # last trade
        ("underlying_bid", pa.float64()),
        ("underlying_ask", pa.float64()),
        ("bid", pa.float64()),
        ("ask", pa.float64()),
        ("bid_size", pa.float64()),
        ("ask_size", pa.float64()),
        ("quote_at", _TS),
        ("last_price", pa.float64()),
        ("last_size", pa.float64()),
        ("trade_at", _TS),
        ("implied_volatility", pa.float64()),
        ("delta", pa.float64()),
        ("gamma", pa.float64()),
        ("theta", pa.float64()),
        ("vega", pa.float64()),
        ("rho", pa.float64()),
        ("open_interest", pa.float64()),
        ("open_interest_date", pa.date32()),
        ("close_price", pa.float64()),
        ("close_price_date", pa.date32()),
        ("style", pa.string()),  # american / european
        ("feed", pa.string()),
        ("ingested_at", _TS),
        ("run_id", pa.string()),
    ]
)


# Brokerage data (read-only). One row per account / holding per sync; activities are the
# broker's transaction history (buys, sells, dividends, contributions, ...).
RAW_BROKER_ACCOUNTS_SCHEMA = pa.schema(
    [
        ("taken_at", _TS),
        ("snapshot_date", pa.date32()),  # New York date of the sync
        ("source", pa.string()),  # snaptrade / alpaca / fake
        ("account_id", pa.string()),
        ("account_name", pa.string()),
        ("account_number_masked", pa.string()),  # last 4 digits only
        ("institution", pa.string()),
        ("cash", pa.float64()),
        ("total_value", pa.float64()),  # as reported by the broker, if it reports one
        ("currency", pa.string()),
        ("ingested_at", _TS),
        ("run_id", pa.string()),
        # How fresh the broker's data is (the aggregator caches it): when positions were last
        # pulled from the brokerage, and the last day of transactions it has. Added later.
        ("holdings_as_of", _TS),
        ("transactions_as_of", pa.date32()),
    ]
)

# kind: stock, etf, adr, mutualfund, cef, option, crypto, cash, other
RAW_BROKER_HOLDINGS_SCHEMA = pa.schema(
    [
        ("taken_at", _TS),
        ("snapshot_date", pa.date32()),
        ("source", pa.string()),
        ("account_id", pa.string()),
        ("symbol", pa.string()),  # options: compact OCC symbol
        ("underlying", pa.string()),  # the symbol itself, or an option's underlying
        ("description", pa.string()),
        ("kind", pa.string()),
        ("quantity", pa.float64()),
        ("price", pa.float64()),  # broker's last price (options: per contract)
        ("market_value", pa.float64()),
        ("cost_basis_per_unit", pa.float64()),  # average cost per unit (options: per contract)
        ("currency", pa.string()),
        ("ingested_at", _TS),
        ("run_id", pa.string()),
    ]
)

RAW_BROKER_ACTIVITIES_SCHEMA = pa.schema(
    [
        ("activity_id", pa.string()),
        ("source", pa.string()),
        ("account_id", pa.string()),
        ("type", pa.string()),  # BUY, SELL, DIVIDEND, CONTRIBUTION, WITHDRAWAL, FEE, ...
        ("symbol", pa.string()),
        ("trade_date", pa.date32()),
        ("settlement_date", pa.date32()),
        ("units", pa.float64()),
        ("price", pa.float64()),
        ("amount", pa.float64()),
        ("fee", pa.float64()),
        ("currency", pa.string()),
        ("description", pa.string()),
        ("ingested_at", _TS),
        ("run_id", pa.string()),
        ("option_action", pa.string()),  # options: BUY_TO_OPEN, SELL_TO_CLOSE, ...; added later
    ]
)


def conform(df: pd.DataFrame, schema: pa.Schema) -> pd.DataFrame:
    """Select and order ``schema``'s columns (adding missing ones as nulls) and cast types."""
    out = pd.DataFrame(index=df.index)
    for field in schema:
        column = df[field.name] if field.name in df.columns else None
        if column is None or column.isna().all():
            # All-null columns arrive with whatever dtype pandas guessed (often float64);
            # make them typeless so Arrow can cast them to any target type.
            column = pd.Series([None] * len(df), index=df.index, dtype=object)
        out[field.name] = column
    table = pa.Table.from_pandas(out, schema=schema, preserve_index=False)
    conformed: pd.DataFrame = table.to_pandas()
    return conformed
