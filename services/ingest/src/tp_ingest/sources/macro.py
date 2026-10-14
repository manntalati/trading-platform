"""Where the macro series come from (``tp_core.macro``), and fallbacks when one is unreachable.

- ``fred-api``: FRED's official API, all five series; needs a free key (``FRED_API_KEY``).
- ``cboe``: Cboe's index history files: the VIX (``VIXCLS``) and the 3-month VIX (``VXVCLS``).
- ``treasury``: the Treasury's daily par yield curve: 3-month, 2- and 10-year (``DGS*``).
- ``fred-csv``: FRED's graph download, all five, no key; it often hangs for non-browser clients.

Cboe and the Treasury are where FRED gets these series, so the numbers are the same. ``auto``
(the default) tries, for each series, the FRED API when a key is set, then the publisher, then the
FRED download; a host that times out or refuses a connection is skipped for the rest of the run,
so one dead host costs one timeout, not one per series.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Protocol

import numpy as np
import pandas as pd
import requests

from tp_core.macro import parse_cboe_csv, parse_fred_csv, parse_fred_json, parse_treasury_csv

FRED_API = "https://api.stlouisfed.org/fred/series/observations"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
CBOE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
CBOE_NAMES = {"VIXCLS": "VIX", "VXVCLS": "VIX3M"}
TREASURY = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)
TIMEOUT = (10.0, 60.0)  # connect, read
USER_AGENT = "trading-platform/0.1 (personal research)"


class MacroSource(Protocol):
    name: str

    def series(self, series_id: str, start: date) -> pd.DataFrame:
        """Observations from ``start`` on: columns ``series, date, value``."""
        ...


def _session(session: requests.Session | None) -> requests.Session:
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", USER_AGENT)
    return s


def _since(df: pd.DataFrame, start: date) -> pd.DataFrame:
    out: pd.DataFrame = df[df["date"] >= start].reset_index(drop=True)
    return out


class FredApiSource:
    """FRED's official API (https://fred.stlouisfed.org/docs/api/api_key.html for a free key)."""

    name = "fred-api"

    def __init__(self, api_key: str, session: requests.Session | None = None) -> None:
        self.api_key = api_key
        self.session = _session(session)

    def series(self, series_id: str, start: date) -> pd.DataFrame:
        response = self.session.get(
            FRED_API,
            params={
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "observation_start": start.isoformat(),
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return parse_fred_json(response.json(), series_id)


class FredCsvSource:
    """FRED's graph download (``fredgraph.csv``), no key."""

    name = "fred-csv"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = _session(session)

    def series(self, series_id: str, start: date) -> pd.DataFrame:
        response = self.session.get(
            FRED_CSV, params={"id": series_id, "cosd": start.isoformat()}, timeout=TIMEOUT
        )
        response.raise_for_status()
        return parse_fred_csv(response.text, series_id)


class CboeSource:
    """Cboe's daily history of the VIX and the 3-month VIX: the whole history in one file."""

    name = "cboe"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = _session(session)

    def series(self, series_id: str, start: date) -> pd.DataFrame:
        if series_id not in CBOE_NAMES:
            raise KeyError(f"Cboe has no {series_id}")
        response = self.session.get(CBOE.format(name=CBOE_NAMES[series_id]), timeout=TIMEOUT)
        response.raise_for_status()
        return _since(parse_cboe_csv(response.text, series_id), start)


class TreasurySource:
    """The Treasury's daily par yield curve, one file per year (fetched once per run and shared
    by the three maturities)."""

    name = "treasury"

    def __init__(self, session: requests.Session | None = None, today: date | None = None) -> None:
        self.session = _session(session)
        self.today = today or datetime.now().astimezone().date()
        self._years: dict[int, dict[str, pd.DataFrame]] = {}

    def _year(self, year: int) -> dict[str, pd.DataFrame]:
        if year not in self._years:
            response = self.session.get(TREASURY.format(year=year), timeout=TIMEOUT)
            response.raise_for_status()
            self._years[year] = parse_treasury_csv(response.text)
        return self._years[year]

    def series(self, series_id: str, start: date) -> pd.DataFrame:
        frames = [self._year(y)[series_id] for y in range(start.year, self.today.year + 1)]
        both = pd.concat([f for f in frames if len(f)], ignore_index=True) if frames else None
        if both is None or both.empty:
            return pd.DataFrame(columns=["series", "date", "value"])
        return _since(both.sort_values("date"), start)


def publisher_for(series_id: str) -> str:
    return "cboe" if series_id in CBOE_NAMES else "treasury"


class MacroUnavailableError(RuntimeError):
    """No source could provide a series; the message lists what each one said."""


class AutoMacroSource:
    """Each series from the first source that answers (see the module notes). ``used`` records
    which one served each series; a source that couldn't be reached is not tried again."""

    name = "auto"

    def __init__(self, sources: dict[str, MacroSource], order: Sequence[str]) -> None:
        self.sources = sources
        self.order = list(order)
        self.used: dict[str, str] = {}
        self.down: dict[str, str] = {}

    @classmethod
    def default(cls, fred_api_key: str | None = None, today: date | None = None) -> AutoMacroSource:
        sources: dict[str, MacroSource] = {
            "cboe": CboeSource(),
            "treasury": TreasurySource(today=today),
            "fred-csv": FredCsvSource(),
        }
        order = ["publisher", "fred-csv"]
        if fred_api_key:
            sources["fred-api"] = FredApiSource(fred_api_key)
            order.insert(0, "fred-api")
        return cls(sources, order)

    def series(self, series_id: str, start: date) -> pd.DataFrame:
        errors = []
        for slot in self.order:
            name = publisher_for(series_id) if slot == "publisher" else slot
            source = self.sources.get(name)
            if source is None:
                continue
            if name in self.down:
                errors.append(f"{name}: skipped ({self.down[name]})")
                continue
            try:
                df = source.series(series_id, start)
            except (requests.ConnectionError, requests.Timeout) as exc:
                self.down[name] = f"unreachable: {type(exc).__name__}"
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            except Exception as exc:  # a bad answer for this series; others may still work
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            self.used[series_id] = name
            return df
        raise MacroUnavailableError("; ".join(errors) or f"no source for {series_id}")


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


__all__ = [
    "AutoMacroSource",
    "CboeSource",
    "FakeMacroSource",
    "FredApiSource",
    "FredCsvSource",
    "MacroSource",
    "MacroUnavailableError",
    "TreasurySource",
]
