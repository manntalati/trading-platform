"""OCC option symbols as used by Alpaca: ``ROOT`` + ``YYMMDD`` + ``C|P`` + 8-digit strike * 1000.

Example: ``SPY241220C00450000`` is the SPY 2024-12-20 450 call. Alpaca omits the space padding
of the full 21-character OSI format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

Right = Literal["C", "P"]

_OCC = re.compile(r"^(?P<root>[A-Z0-9]{1,6})(?P<ymd>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")


@dataclass(frozen=True)
class OccSymbol:
    root: str
    expiration: date
    right: Right
    strike: float

    def __str__(self) -> str:
        return format_occ(self.root, self.expiration, self.right, self.strike)


def parse_occ(symbol: str) -> OccSymbol:
    match = _OCC.match(symbol.replace(" ", ""))
    if match is None:
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    ymd = match["ymd"]
    expiration = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    right: Right = "C" if match["right"] == "C" else "P"
    return OccSymbol(match["root"], expiration, right, int(match["strike"]) / 1000)


def format_occ(root: str, expiration: date, right: Right, strike: float) -> str:
    strike_thousandths = round(strike * 1000)
    if not 0 < strike_thousandths < 100_000_000:
        raise ValueError(f"strike out of OCC range: {strike}")
    return f"{root}{expiration:%y%m%d}{right}{strike_thousandths:08d}"
