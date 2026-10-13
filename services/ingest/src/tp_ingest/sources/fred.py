"""FRED (Federal Reserve Bank of St. Louis) daily series, through the public CSV download.

``https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS&cosd=2026-01-01`` needs no API key.
Parsing is ``tp_core.macro.parse_fred_csv``; this module only fetches.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable
from datetime import date
from typing import Protocol

import numpy as np
import pandas as pd
import requests

from tp_core.macro import parse_fred_csv

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


class MacroSource(Protocol):
    name: str

    def series(self, series_id: str, start: date) -> pd.DataFrame:
        """Observations from ``start`` on: columns ``series, date, value``."""
        ...


class FredSource:
    name = "fred"

    def __init__(
        self, session: requests.Session | None = None, timeout: float = 30.0, url: str = URL
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.url = url

    def series(self, series_id: str, start: date) -> pd.DataFrame:
        response = self.session.get(
            self.url,
            params={"id": series_id, "cosd": start.isoformat()},
            timeout=self.timeout,
            headers={"User-Agent": "trading-platform (personal research)"},
        )
        response.raise_for_status()
        return parse_fred_csv(response.text, series_id)


class FakeMacroSource:
    """Deterministic synthetic series for dry runs and tests: a VIX that mean-reverts around 18,
    its 3-month version a little above, and yields that drift slowly. Weekends are missing, as
    in FRED."""

    name = "fake"

    def __init__(self, end: date | None = None, fail: set[str] | None = None) -> None:
        self.end = end or date(2026, 10, 9)
        self.fail = fail or set()

    def series(self, series_id: str, start: date) -> pd.DataFrame:
        if series_id in self.fail:
            raise requests.HTTPError(f"fake failure for {series_id}")
        days = pd.bdate_range(start, self.end)
        rng = np.random.default_rng(zlib.crc32(series_id.encode()))
        shape: dict[str, Callable[[int], float]] = {
            "VIXCLS": lambda i: 18 + 6 * math.sin(i / 90),
            "VXVCLS": lambda i: 20 + 4 * math.sin(i / 90),
            "DGS3MO": lambda i: 3.5 + 1.5 * math.sin(i / 700),
            "DGS2": lambda i: 3.7 + 1.2 * math.sin(i / 650),
            "DGS10": lambda i: 4.0 + 0.8 * math.sin(i / 600),
        }
        base = shape.get(series_id, _flat)
        values = [round(base(i) + float(rng.normal(0, 0.05)), 2) for i in range(len(days))]
        return pd.DataFrame({"series": series_id, "date": days.date, "value": values})


def _flat(i: int) -> float:
    return 1.0
