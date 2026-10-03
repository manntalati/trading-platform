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
