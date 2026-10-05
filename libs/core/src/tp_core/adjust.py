"""Explicit split and dividend adjustment.

Backward adjustment: the most recent bar is left as traded and every earlier bar is scaled so
that returns across corporate actions are economic returns rather than artefacts.

For a bar on session ``s``, ``price_adj = price_raw * split_factor(s) * div_factor(s)`` where each
factor is the product over events whose ex-date is *after* ``s``:

- split with ratio ``r = new_rate / old_rate`` (4-for-1 ⇒ 4, 1-for-10 reverse ⇒ 0.1):
  price factor ``1 / r``, volume factor ``r``;
- stock dividend of ``k`` extra shares per share: a split with ``r = 1 + k``;
- cash dividend ``D`` per share: price factor ``1 - D / close_prev``, where ``close_prev`` is the
  raw close on the last session before the ex-date (the CRSP / Yahoo convention).

Only events with an ex-date inside the bar history are applied: an announced future dividend
must not change today's prices (that would be look-ahead).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

SPLIT_TYPES = frozenset({"forward_split", "reverse_split"})


@dataclass(frozen=True)
class AdjustmentProblem:
    symbol: str
    ex_date: date
    detail: str


def split_ratio(action: pd.Series) -> float | None:
    """Shares after / shares before for a split-like action, or None if not split-like."""
    kind = action["action_type"]
    if kind in SPLIT_TYPES:
        new, old = action["new_rate"], action["old_rate"]
        if pd.isna(new) or pd.isna(old) or new <= 0 or old <= 0:
            return None
        return float(new) / float(old)
    if kind == "stock_dividend":
        rate = action["rate"]
        if pd.isna(rate) or rate <= 0:
            return None
        return 1.0 + float(rate)
    return None


def adjust_symbol(
    bars: pd.DataFrame, actions: pd.DataFrame
) -> tuple[pd.DataFrame, list[AdjustmentProblem]]:
    """Add ``split_factor``, ``div_factor`` and ``adj_*`` columns to one symbol's bars.

    ``bars`` must contain a single symbol with ``session``, OHLC and ``volume`` columns.
    ``actions`` holds that symbol's corporate actions (any order, may be empty).
    """
    out = bars.sort_values("session").reset_index(drop=True)
    symbol = str(out["symbol"].iloc[0]) if len(out) else ""
    sessions = pd.to_datetime(out["session"]).to_numpy()
    closes = out["close"].to_numpy(dtype=float)
    split_factor = np.ones(len(out))
    div_factor = np.ones(len(out))
    problems: list[AdjustmentProblem] = []

    if len(out):
        first, last = sessions[0], sessions[-1]
        for _, action in actions.iterrows():
            ex = pd.Timestamp(action["ex_date"]).to_datetime64()
            # Events at or before the first bar change nothing; events after the last bar are
            # in the future relative to this data and must not be applied yet.
            if ex <= first or ex > last:
                continue
            before = sessions < ex
            if action["action_type"] == "cash_dividend":
                amount = action["rate"]
                prev_close = closes[before][-1]
                if pd.isna(amount) or amount <= 0 or amount >= prev_close:
                    problems.append(
                        AdjustmentProblem(
                            symbol,
                            pd.Timestamp(ex).date(),
                            f"unusable cash dividend {amount!r} vs prior close {prev_close}",
                        )
                    )
                    continue
                div_factor[before] *= 1.0 - float(amount) / prev_close
            else:
                ratio = split_ratio(action)
                if ratio is None:
                    problems.append(
                        AdjustmentProblem(
                            symbol,
                            pd.Timestamp(ex).date(),
                            f"unusable {action['action_type']}: {action.to_dict()}",
                        )
                    )
                    continue
                split_factor[before] /= ratio

    out["split_factor"] = split_factor
    out["div_factor"] = div_factor
    price_factor = split_factor * div_factor
    for col in ("open", "high", "low", "close"):
        out[f"adj_{col}"] = out[col].to_numpy(dtype=float) * price_factor
    out["adj_volume"] = out["volume"].to_numpy(dtype=float) / split_factor
    return out, problems
