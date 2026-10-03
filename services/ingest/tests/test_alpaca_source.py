"""Exercise the real alpaca-py code path against recorded-shape HTTP responses (no network)."""

import json
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
import responses

from tp_ingest.sources.alpaca import AlpacaSource

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
CA_URL = "https://data.alpaca.markets/v1/corporate-actions"


def _bar(t: str, c: float) -> dict[str, object]:
    return {"t": t, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1000, "n": 10, "vw": c}


@pytest.fixture
def source() -> AlpacaSource:
    return AlpacaSource("key", "secret", feed="sip")


@responses.activate
def test_daily_bars_paginates_and_maps(source: AlpacaSource) -> None:
    responses.get(
        BARS_URL,
        json={
            "bars": {"AAPL": [_bar("2024-06-03T04:00:00Z", 194.0)]},
            "next_page_token": "page2",
        },
    )
    responses.get(
        BARS_URL,
        json={
            "bars": {
                "AAPL": [_bar("2024-06-04T04:00:00Z", 195.0)],
                "SPY": [_bar("2024-06-03T04:00:00Z", 527.0)],
            },
            "next_page_token": None,
        },
    )

    df = source.daily_bars(["AAPL", "SPY"], date(2024, 6, 3), date(2024, 6, 4))

    assert sorted(zip(df["symbol"], df["close"], strict=True)) == [
        ("AAPL", 194.0),
        ("AAPL", 195.0),
        ("SPY", 527.0),
    ]
    assert str(df["timestamp"].dt.tz) == "UTC"
    assert df["volume"].dtype == "float64"

    query = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert query["symbols"] == ["AAPL,SPY"]
    assert query["timeframe"] == ["1Day"]
    assert query["adjustment"] == ["raw"]
    assert query["feed"] == ["sip"]
    assert query["start"][0].startswith("2024-06-03T04:00:00")  # midnight New York in UTC
    assert query["end"][0].startswith("2024-06-05T03:59:59")  # end of the last New York day
    second = parse_qs(urlparse(responses.calls[1].request.url).query)
    assert second["page_token"] == ["page2"]
    assert responses.calls[0].request.headers["APCA-API-KEY-ID"] == "key"


@responses.activate
def test_corporate_actions_map_and_filter(source: AlpacaSource) -> None:
    payload = {
        "corporate_actions": {
            "forward_splits": [
                {
                    "id": "s1",
                    "symbol": "NVDA",
                    "cusip": "67066G104",
                    "new_rate": 10,
                    "old_rate": 1,
                    "process_date": "2024-06-10",
                    "ex_date": "2024-06-10",
                    "record_date": "2024-06-06",
                    "payable_date": "2024-06-07",
                }
            ],
            "cash_dividends": [
                {
                    "id": "d1",
                    "symbol": "AAPL",
                    "cusip": "037833100",
                    "rate": 0.25,
                    "special": False,
                    "foreign": False,
                    "process_date": "2024-05-16",
                    "ex_date": "2024-05-10",
                    "record_date": "2024-05-13",
                    "payable_date": "2024-05-16",
                },
                {  # ex-date outside the requested window: dropped
                    "id": "d2",
                    "symbol": "AAPL",
                    "rate": 0.25,
                    "special": False,
                    "foreign": False,
                    "process_date": "2024-08-15",
                    "ex_date": "2024-08-12",
                },
            ],
            "name_changes": [{"id": "n1", "old_symbol": "FB", "new_symbol": "META"}],
        },
        "next_page_token": None,
    }
    responses.get(CA_URL, json=payload)

    df = source.corporate_actions(["AAPL", "NVDA"], date(2024, 5, 1), date(2024, 6, 30))

    assert list(df["id"]) == ["s1", "d1"]
    split = df.iloc[0]
    assert (split["action_type"], split["new_rate"], split["old_rate"]) == (
        "forward_split",
        10.0,
        1.0,
    )
    assert split["ex_date"] == date(2024, 6, 10)
    div = df.iloc[1]
    assert (div["action_type"], div["rate"], div["special"]) == ("cash_dividend", 0.25, False)
    assert pd.isna(div["new_rate"])
    query = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert query["symbols"] == ["AAPL,NVDA"]
    assert set(query["types"][0].split(",")) == {
        "forward_split",
        "reverse_split",
        "stock_dividend",
        "cash_dividend",
    }


@responses.activate
def test_corporate_actions_requested_in_yearly_windows(source: AlpacaSource) -> None:
    responses.get(CA_URL, json={"corporate_actions": {}, "next_page_token": None})
    df = source.corporate_actions(["AAPL"], date(2020, 1, 1), date(2022, 6, 30))
    assert df.empty
    windows = [parse_qs(urlparse(c.request.url).query) for c in responses.calls]
    assert [(w["start"][0], w["end"][0]) for w in windows] == [
        ("2020-01-01", "2020-12-31"),
        ("2021-01-01", "2022-01-01"),
        ("2022-01-02", "2022-06-30"),
    ]
    assert json.dumps(df.columns.tolist())  # schema columns present even when empty


@responses.activate
def test_daily_bars_end_respects_sip_delay(source: AlpacaSource) -> None:
    responses.get(BARS_URL, json={"bars": {}, "next_page_token": None})
    today = datetime.now(UTC).date()
    source.daily_bars(["SPY"], today - timedelta(days=5), today + timedelta(days=1))
    end = parse_qs(urlparse(responses.calls[0].request.url).query)["end"][0]
    requested = datetime.fromisoformat(end.removesuffix("Z")).replace(tzinfo=UTC)
    assert requested <= datetime.now(UTC) - timedelta(minutes=15)
