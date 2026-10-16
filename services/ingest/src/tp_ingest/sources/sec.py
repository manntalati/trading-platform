"""SEC EDGAR: the ticker-to-CIK map, company facts and submissions (``tp_core.fundamentals``).

Free and keyless, with two rules (https://www.sec.gov/os/accessing-edgar-data): every request
names the client in its User-Agent (a name and an email address, ``TP_SEC_USER_AGENT``), and no
more than ten requests a second. This client waits ``pause`` seconds between requests.
"""

from __future__ import annotations

import time
import zlib
from collections.abc import Mapping
from typing import Any, Protocol

import requests

from tp_core.testing import synthetic_company_facts, synthetic_submissions

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
EXTRA_URL = "https://data.sec.gov/submissions/{name}"


class FundamentalsSource(Protocol):
    name: str

    def tickers(self) -> dict[str, int]:
        """Ticker (as EDGAR writes it: ``BRK-B``) to CIK."""
        ...

    def company_facts(self, cik: int) -> dict[str, Any]: ...

    def submissions(self, cik: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The submissions document and its older pages."""
        ...


def edgar_ticker(symbol: str) -> str:
    """``BRK.B`` (Alpaca) is ``BRK-B`` on EDGAR."""
    return symbol.upper().replace(".", "-")


class SecSource:
    name = "sec"

    def __init__(
        self, user_agent: str, session: requests.Session | None = None, pause: float = 0.15
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.pause = pause
        self._last = 0.0

    def _get(self, url: str) -> Any:
        wait = self._last + self.pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        response = self.session.get(url, timeout=60)
        response.raise_for_status()
        return response.json()

    def tickers(self) -> dict[str, int]:
        doc: Mapping[str, Mapping[str, Any]] = self._get(TICKERS_URL)
        return {str(v["ticker"]).upper(): int(v["cik_str"]) for v in doc.values()}

    def company_facts(self, cik: int) -> dict[str, Any]:
        try:
            out: dict[str, Any] = self._get(FACTS_URL.format(cik=cik))
        except requests.HTTPError as exc:
            # EDGAR answers 404 for filers without XBRL financial data, such as the trusts behind
            # many ETFs (they are in the ticker list all the same): no facts, not a failure
            if exc.response is not None and exc.response.status_code == 404:
                return {"cik": cik, "facts": {}}
            raise
        return out

    def submissions(self, cik: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        doc: dict[str, Any] = self._get(SUBMISSIONS_URL.format(cik=cik))
        files = (doc.get("filings") or {}).get("files") or []
        extra = [self._get(EXTRA_URL.format(name=f["name"])) for f in files]
        return doc, extra


class FakeSecSource:
    """Synthetic filings for dry runs and tests: every symbol not in ``missing`` is a company
    with a CIK from its name, five years of growing quarterly EPS with a surprise now and then,
    and an earnings release before each 10-Q."""

    name = "fake"

    def __init__(
        self,
        symbols: list[str],
        missing: set[str] | None = None,
        last_year: int = 2025,
        years: int = 5,
    ) -> None:
        self.symbols = [s for s in symbols if s not in (missing or set())]
        self.last_year = last_year
        self.years = years

    def tickers(self) -> dict[str, int]:
        return {edgar_ticker(s): zlib.crc32(s.encode()) % 9_000_000 + 1 for s in self.symbols}

    def _eps(self, cik: int) -> dict[tuple[int, int], float]:
        base = 1.0 + (cik % 7) / 10
        out = {}
        for y in range(self.last_year - self.years + 1, self.last_year + 1):
            for q in (1, 2, 3, 4):
                bump = 0.4 if (cik + y * 4 + q) % 9 == 0 else 0.0
                growth = 1 + 0.08 * (y - self.last_year + self.years - 1)
                out[(y, q)] = round(base * growth + 0.02 * q + bump, 2)
        return out

    def company_facts(self, cik: int) -> dict[str, Any]:
        return synthetic_company_facts(cik, self._eps(cik))

    def submissions(self, cik: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return synthetic_submissions(cik, sorted(self._eps(cik))), []
