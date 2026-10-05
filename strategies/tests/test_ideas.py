import json

import numpy as np
import pandas as pd
import pytest

from tp_core.portfolio import Classifier
from tp_strategies.ideas import Limits, generate_ideas, symbol_signals

DAYS = pd.bdate_range("2022-01-03", periods=520)
CLASSIFIER = Classifier(
    sectors={
        "BIG": "Information Technology",
        "DOWN": "Information Technology",
        "UPTR": "Information Technology",
        "HEAL": "Health Care",
    },
    funds={
        "SPY": "US Equity",
        "EFA": "International Equity",
        "IEF": "US Treasuries",
        "VNQ": "Real Estate",
        "DBC": "Commodities",
    },
)


def walk(seed: int, drift: float, vol: float = 0.01, crash_at: int | None = None) -> np.ndarray:
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, len(DAYS))
    if crash_at is not None:
        r[crash_at:] -= 0.004  # a long slide from here on
    return 100 * np.exp(np.cumsum(r))


@pytest.fixture
def prices() -> pd.DataFrame:
    market = np.random.default_rng(99).normal(0.0004, 0.01, len(DAYS))
    spy = 100 * np.exp(np.cumsum(market))
    return pd.DataFrame(
        {
            "SPY": spy,
            "BIG": 100 * np.exp(np.cumsum(1.6 * market + 0.0003)),  # high beta to SPY
            "DOWN": walk(2, 0.001, crash_at=330),
            "UPTR": walk(3, 0.002),
            "HEAL": walk(4, 0.0015, vol=0.008),
            "EFA": walk(5, 0.0002),
            "IEF": walk(6, 0.0001, vol=0.004),
            "VNQ": walk(7, 0.0002),
            "DBC": walk(8, 0.0001),
        },
        index=DAYS,
    )


def table(rows: list[tuple[str, str, float, float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["symbol", "kind", "quantity", "market_value"])
    df["weight"] = df["market_value"] / df["market_value"].sum()
    classes = [CLASSIFIER.classify(s, k) for s, k in zip(df["symbol"], df["kind"], strict=True)]
    df["sector"] = [c.sector for c in classes]
    df["asset_class"] = [c.asset_class for c in classes]
    return df


@pytest.fixture
def holdings() -> pd.DataFrame:
    return table(
        [
            ("BIG", "stock", 300, 60_000),
            ("DOWN", "stock", 50, 25_000),
            ("SPY", "etf", 20, 10_000),
            ("FUND", "mutualfund", 10, 5_000),
        ]
    )


def titles(report, kind: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [i.title for i in report.by_kind(kind)]


def test_signals_table(prices: pd.DataFrame) -> None:
    sig = symbol_signals(prices)
    assert bool(sig.loc["UPTR", "above_10m_sma"]) is True
    assert bool(sig.loc["DOWN", "above_10m_sma"]) is False
    assert sig.loc["UPTR", "momentum_12_1"] > 0
    assert sig.loc["DOWN", "drawdown_52w"] < -0.25


def test_holding_ideas(prices: pd.DataFrame, holdings: pd.DataFrame) -> None:
    report = generate_ideas(holdings, prices, CLASSIFIER)
    held = titles(report, "holding")
    assert "DOWN: trend broken" in held
    assert any(t.startswith("DOWN:") and "52-week high" in t for t in held)
    assert "BIG: covered-call eligible" in held
    broken = next(i for i in report.ideas if i.title == "DOWN: trend broken")
    assert broken.severity == "attention"
    assert any("10-month average" in r for r in broken.rationale)


def test_portfolio_ideas(prices: pd.DataFrame, holdings: pd.DataFrame) -> None:
    report = generate_ideas(holdings, prices, CLASSIFIER)
    port = titles(report, "portfolio")
    assert "BIG is 60% of the portfolio" in port
    assert "DOWN is 25% of the portfolio" in port
    assert "Information Technology is 85% of the portfolio" in port
    assert any(t.startswith("95% in US equity") or "US equity" in t for t in port)
    assert not any(t.startswith("Beta ") for t in port)  # (0.6 x 1.6 + 0.1) / 0.95 ~ 1.1
    assert not any("isn't price-analysed" in t for t in port)  # FUND is only 5%


def test_high_beta_and_unpriced_share(prices: pd.DataFrame) -> None:
    holdings = table([("BIG", "stock", 10, 70_000), ("FUND", "mutualfund", 10, 30_000)])
    port = titles(generate_ideas(holdings, prices, CLASSIFIER), "portfolio")
    beta = next(t for t in port if t.startswith("Beta "))
    assert float(beta.split()[1]) == pytest.approx(1.6, abs=0.15)
    assert "30% of the portfolio isn't price-analysed" in port
    assert not any(t.startswith("FUND is") for t in port)  # funds are exempt from the cap


def test_strategy_ideas(prices: pd.DataFrame, holdings: pd.DataFrame) -> None:
    report = generate_ideas(holdings, prices, CLASSIFIER)
    strat = titles(report, "strategy")
    assert "10-month MA timing on your current mix" in strat
    assert "Faber GTAA as a diversifier" in strat
    overlay = next(i for i in report.ideas if i.title.startswith("10-month MA timing"))
    assert any(r.startswith("Max drawdown") for r in overlay.rationale)


def test_candidates_exclude_holdings_and_favour_gaps(
    prices: pd.DataFrame, holdings: pd.DataFrame
) -> None:
    report = generate_ideas(holdings, prices, CLASSIFIER)
    cands = report.by_kind("candidate")
    symbols = [i.symbols[0] for i in cands]
    assert "HEAL" in symbols
    assert not {"BIG", "DOWN", "SPY"} & set(symbols)
    heal = next(i for i in cands if i.symbols == ["HEAL"])
    assert any("Health Care: under 10%" in r for r in heal.rationale)
    assert "diversify" in heal.title
    assert [i.score for i in cands] == sorted((i.score for i in cands), reverse=True)


def test_severity_ordering_and_json(prices: pd.DataFrame, holdings: pd.DataFrame) -> None:
    report = generate_ideas(holdings, prices, CLASSIFIER)
    order = {"attention": 0, "consider": 1, "info": 2}
    ranks = [order[i.severity] for i in report.ideas]
    assert ranks == sorted(ranks)
    payload = json.dumps(report.to_dict(), allow_nan=False)
    assert "Not investment advice" in payload


def test_empty_portfolio_still_gets_candidates(prices: pd.DataFrame) -> None:
    report = generate_ideas(table([]), prices, CLASSIFIER)
    assert {i.kind for i in report.ideas} == {"candidate"}


def test_limits_are_configurable(prices: pd.DataFrame, holdings: pd.DataFrame) -> None:
    report = generate_ideas(holdings, prices, CLASSIFIER, limits=Limits(max_position=0.7))
    assert not any(t.startswith("BIG is") for t in titles(report, "portfolio"))
