from datetime import date

import numpy as np
import pandas as pd
import pytest

from tp_core.adjust import adjust_symbol
from tp_core.testing import actions, raw_bars

# Sessions from 2024-06-03: Mon 3, Tue 4, Wed 5, Thu 6, Fri 7.
D = [date(2024, 6, d) for d in (3, 4, 5, 6, 7)]


def _bars(closes: list[float]) -> pd.DataFrame:
    return raw_bars("XYZ", closes, start=D[0])


def test_no_actions_is_identity() -> None:
    out, problems = adjust_symbol(_bars([10, 11, 12]), actions())
    assert problems == []
    np.testing.assert_allclose(out["adj_close"], [10, 11, 12])
    np.testing.assert_allclose(out["adj_volume"], out["volume"])


def test_forward_split_scales_history_before_ex_date() -> None:
    split = actions(
        {
            "symbol": "XYZ",
            "action_type": "forward_split",
            "ex_date": D[2],
            "new_rate": 2.0,
            "old_rate": 1.0,
        }
    )
    out, problems = adjust_symbol(_bars([100, 102, 51, 52]), split)
    assert problems == []
    np.testing.assert_allclose(out["split_factor"], [0.5, 0.5, 1, 1])
    np.testing.assert_allclose(out["adj_close"], [50, 51, 51, 52])
    np.testing.assert_allclose(out["adj_volume"], [2e6, 2e6, 1e6, 1e6])
    np.testing.assert_allclose(out["close"], [100, 102, 51, 52])  # raw kept as traded


def test_reverse_split() -> None:
    reverse = actions(
        {
            "symbol": "XYZ",
            "action_type": "reverse_split",
            "ex_date": D[1],
            "new_rate": 1.0,
            "old_rate": 10.0,
        }
    )
    out, _ = adjust_symbol(_bars([1.0, 10.5]), reverse)
    np.testing.assert_allclose(out["adj_close"], [10.0, 10.5])


def test_stock_dividend_is_split_like() -> None:
    stock_div = actions(
        {"symbol": "XYZ", "action_type": "stock_dividend", "ex_date": D[1], "rate": 0.25}
    )
    out, _ = adjust_symbol(_bars([125, 100]), stock_div)
    np.testing.assert_allclose(out["adj_close"], [100, 100])


def test_cash_dividend_uses_prior_close() -> None:
    div = actions({"symbol": "XYZ", "action_type": "cash_dividend", "ex_date": D[2], "rate": 1.0})
    out, _ = adjust_symbol(_bars([100, 100, 99, 99]), div)
    np.testing.assert_allclose(out["div_factor"], [0.99, 0.99, 1, 1])
    # The ex-date drop of exactly the dividend is not a loss once adjusted.
    assert out["adj_close"].pct_change().iloc[2] == pytest.approx(0.0)


def test_split_and_dividend_compound() -> None:
    acts = actions(
        {
            "symbol": "XYZ",
            "action_type": "forward_split",
            "ex_date": D[1],
            "new_rate": 2.0,
            "old_rate": 1.0,
        },
        {"symbol": "XYZ", "action_type": "cash_dividend", "ex_date": D[2], "rate": 0.5},
    )
    out, _ = adjust_symbol(_bars([100, 50, 49.5]), acts)
    np.testing.assert_allclose(out["split_factor"], [0.5, 1, 1])
    np.testing.assert_allclose(out["div_factor"], [0.99, 0.99, 1])
    np.testing.assert_allclose(out["adj_close"], [49.5, 49.5, 49.5])


@pytest.mark.parametrize("ex_date", [D[0], date(2024, 5, 1), date(2024, 6, 10)])
def test_events_outside_history_are_ignored(ex_date: date) -> None:
    """At/before the first bar changes nothing; after the last bar would be look-ahead."""
    div = actions(
        {"symbol": "XYZ", "action_type": "cash_dividend", "ex_date": ex_date, "rate": 1.0}
    )
    out, problems = adjust_symbol(_bars([100, 101, 102]), div)
    assert problems == []
    np.testing.assert_allclose(out["adj_close"], [100, 101, 102])


def test_unusable_dividend_is_reported_not_applied() -> None:
    div = actions({"symbol": "XYZ", "action_type": "cash_dividend", "ex_date": D[1], "rate": 500.0})
    out, problems = adjust_symbol(_bars([100, 101]), div)
    assert len(problems) == 1
    assert problems[0].ex_date == D[1]
    np.testing.assert_allclose(out["adj_close"], [100, 101])


def test_adjustment_is_order_independent() -> None:
    bars = _bars([100, 102, 51, 52])
    shuffled = bars.sample(frac=1.0, random_state=0)
    split = actions(
        {
            "symbol": "XYZ",
            "action_type": "forward_split",
            "ex_date": D[2],
            "new_rate": 2.0,
            "old_rate": 1.0,
        }
    )
    a, _ = adjust_symbol(bars, split)
    b, _ = adjust_symbol(shuffled, split)
    pd.testing.assert_frame_equal(a, b)
