from datetime import date

import pytest

from tp_core.occ import OccSymbol, format_occ, parse_occ


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("SPY241220C00450000", OccSymbol("SPY", date(2024, 12, 20), "C", 450.0)),
        ("AAPL250117P00187500", OccSymbol("AAPL", date(2025, 1, 17), "P", 187.5)),
        ("SPXW261030C05800000", OccSymbol("SPXW", date(2026, 10, 30), "C", 5800.0)),
        ("AAPL1 250117P00001500", OccSymbol("AAPL1", date(2025, 1, 17), "P", 1.5)),
    ],
)
def test_parse(symbol: str, expected: OccSymbol) -> None:
    assert parse_occ(symbol) == expected


def test_round_trip() -> None:
    symbol = "QQQ270115P00512500"
    assert str(parse_occ(symbol)) == symbol
    assert format_occ("QQQ", date(2027, 1, 15), "P", 512.5) == symbol


@pytest.mark.parametrize("bad", ["SPY", "SPY241220X00450000", "spy241220C00450000", ""])
def test_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match="OCC"):
        parse_occ(bad)
