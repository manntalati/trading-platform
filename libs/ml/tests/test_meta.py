from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tp_ml import meta
from tp_ml.cv import uniqueness
from tp_ml.models import QuantileBins, make_model


def synthetic(n: int = 1500, signal: float = 1.0, seed: int = 0) -> pd.DataFrame:
    """A labelled dataset shaped like ``meta.dataset``'s: random features, and a label that
    depends on ``ret_5`` (with ``signal`` > 0) or on nothing."""
    rng = np.random.default_rng(seed)
    x = pd.DataFrame(rng.normal(size=(n, len(meta.PRICE_FEATURES))), columns=meta.PRICE_FEATURES)
    label = (signal * x["ret_5"] + rng.normal(size=n) > 0).astype(int)
    days = pd.bdate_range("2018-01-01", periods=n)
    out = x.assign(
        symbol="XYZ",
        date=days,
        start=days + pd.offsets.BDay(1),
        end=days + pd.offsets.BDay(4),
        label=label,
        ret=np.where(label == 1, 0.01, -0.01),
    )
    out["weight"] = uniqueness(out["start"], out["end"], days.union(days + pd.offsets.BDay(4)))
    return out


@pytest.mark.parametrize("model", ["logistic", "gbm"])
def test_a_real_signal_is_found_out_of_sample(model: str) -> None:
    cfg = meta.MetaConfig(model=model, threshold=0.5)
    report = meta.evaluate(synthetic(signal=1.0), cfg)
    for name in ("cv", "walk_forward"):
        scores = report[name]
        assert scores["auc"] > 0.68
        assert scores["precision_kept"] > scores["precision_all"] + 0.1
        assert scores["avg_trade_kept"] > scores["avg_trade_all"]


@pytest.mark.parametrize("model", ["logistic", "gbm"])
def test_noise_scores_a_coin_flip(model: str) -> None:
    """The leak check: with nothing to learn, out-of-fold AUC must sit near 0.5."""
    report = meta.evaluate(synthetic(signal=0.0, seed=3), meta.MetaConfig(model=model))
    for name in ("cv", "walk_forward"):
        assert abs(report[name]["auc"] - 0.5) < 0.06


def test_empty_and_constant_columns_are_left_out() -> None:
    data = synthetic(400).assign(rel_sector_63=np.nan, rsi_14=5.0)
    used = meta.usable_columns(data, meta.PRICE_FEATURES)
    assert "rel_sector_63" not in used
    assert "rsi_14" not in used
    assert "ret_5" in used
    # an all-missing column used to crash the gradient boosting
    report = meta.evaluate(data, meta.MetaConfig(model="gbm"))
    assert "rel_sector_63" not in report["columns"]
    assert report["cv"]["auc"] > 0.6


def test_fit_and_predict() -> None:
    data = synthetic(600)
    model = meta.fit(data, meta.MetaConfig(model="logistic"))
    assert model.events == 600
    assert model.trained_to == data["end"].max()
    p = model.predict(data)
    assert p.shape == (600,)
    assert ((p > 0) & (p < 1)).all()
    assert np.corrcoef(p, data["ret_5"])[0, 1] > 0.5
    with pytest.raises(ValueError, match="same label"):
        meta.fit(data.assign(label=1), meta.MetaConfig())


def test_bet_size() -> None:
    sizes = meta.bet_size([0.3, 0.5, 0.55, 0.7, 0.99])
    assert sizes[0] == sizes[1] == 0
    assert 0 < sizes[2] < sizes[3] < sizes[4] <= 1
    assert sizes[4] > 0.95


def test_rsi2_events_are_dips_in_uptrends() -> None:
    days = pd.bdate_range("2023-01-02", periods=260)
    up = pd.Series(np.linspace(100, 150, 260), index=days)
    up.iloc[250:252] = [up.iloc[249] * 0.98, up.iloc[249] * 0.96]  # a two-day drop
    down = pd.Series(np.linspace(150, 100, 260), index=days)
    down.iloc[250:252] = [down.iloc[249] * 0.98, down.iloc[249] * 0.96]
    events = meta.rsi2_events(pd.DataFrame({"UP": up, "DOWN": down}))
    assert set(events["symbol"]) == {"UP"}
    assert events["date"].min() == days[250]
    assert (events["side"] == 1).all()


def test_train_on_bars(bars: dict[str, pd.DataFrame]) -> None:
    cfg = meta.MetaConfig(model="logistic")
    model = meta.train(bars, cfg, min_events=20)
    assert model is not None
    assert model.trained_to <= bars["close"].index[-1]
    assert set(model.columns) <= set(meta.PRICE_FEATURES)
    assert meta.train(bars, cfg, min_events=100_000) is None


@pytest.mark.parametrize("label", ["trade", "barrier"])
def test_dataset_rows_are_labelled_signals(bars: dict[str, pd.DataFrame], label: str) -> None:
    from tp_ml.features import compute_features

    cfg = meta.MetaConfig(label=label)
    events = meta.rsi2_events(bars["close"])
    data = meta.dataset(events, compute_features(bars), bars, cfg)
    assert 0 < len(data) <= len(events)
    assert {"label", "ret", "start", "end", "weight", *meta.PRICE_FEATURES} <= set(data.columns)
    assert data["weight"].between(0, 1).all()
    assert (data["start"] > data["date"]).all()


def test_unknown_names_raise() -> None:
    with pytest.raises(ValueError, match="unknown model"):
        make_model("forest")
    events = pd.DataFrame(columns=["symbol", "date"])
    with pytest.raises(ValueError, match="unknown label"):
        meta.label_events(events, {}, meta.MetaConfig(label="nope"))


def test_report_text() -> None:
    report = meta.evaluate(synthetic(600), meta.MetaConfig(model="logistic"))
    text = meta.report_text(report)
    for phrase in ("600 RSI(2) signals", "made money", "Purged 5-fold: AUC", "Walk-forward: AUC"):
        assert phrase in text


def test_quantile_bins_keep_missing_values_missing() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(1000, 2))
    x[:10, 0] = np.nan
    x[10, 1] = np.inf
    bins = QuantileBins(8).fit(x)
    out = bins.transform(x)
    assert np.isnan(out[:10, 0]).all()
    assert np.isnan(out[10, 1])
    finite = out[np.isfinite(out)]
    assert (finite.min(), finite.max()) == (0, 7)
    assert len(np.unique(out[:, 1][np.isfinite(out[:, 1])])) == 8
    # bins come from the training data: new extremes land in the end bins
    assert bins.transform(np.array([[-100.0, 100.0]])).tolist() == [[0.0, 7.0]]
