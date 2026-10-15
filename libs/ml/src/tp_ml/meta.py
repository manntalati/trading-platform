"""Meta-labeling (López de Prado, ch. 3): a rule-based strategy proposes, a model decides.

The primary strategy (RSI(2) mean reversion, strategy 5) says *which way and when*: buy names in
an uptrend after a sharp two-day drop. Every one of its signals becomes an event; the triple
barrier labels it 1 if the trade would have reached its profit target before its stop. A
classifier then learns, from the point-in-time features on the signal day, which signals tend to
work, and the strategy takes only those, sized by the model's confidence.

The point is precision: the primary strategy keeps its economic logic and its recall, the model
removes the trades least likely to pay. ``evaluate`` measures that honestly: every probability
is out-of-fold under purged, embargoed cross-validation (and a stricter walk-forward), and the
report compares the trades the model would keep with all of them.

Two labels: ``trade`` (the default) is the trade RSI(2)'s own exits make (sell the open after a
close above the 5-day average, or after 10 sessions); ``barrier`` is the triple barrier, a
profit target and a stop in daily volatilities.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from tp_core.indicators import wilder_rsi
from tp_ml.cv import PurgedKFold, oof_predict, uniqueness, walk_forward, weight_arg
from tp_ml.features import compute_features
from tp_ml.labels import rule_labels, triple_barrier
from tp_ml.models import make_model

PRICE_FEATURES = (
    "ret_1", "ret_5", "ret_21", "ret_63", "ret_126", "rel_market_63", "rel_sector_63",
    "vol_21", "vol_63", "vol_ratio", "atr_pct", "range_pct", "volume_z", "dollar_volume",
    "dist_ma20", "dist_ma50", "dist_ma200", "rsi_2", "rsi_14",
)  # fmt: skip
LABELS = ("trade", "barrier")


@dataclass(frozen=True)
class MetaConfig:
    entry: float = 10.0  # RSI(2) below this is a signal
    trend: int = 200  # only above the 200-day average
    label: str = "trade"  # or "barrier"
    exit_ma: int = 5  # trade label: sell after a close above this average
    max_hold: int = 10  # trade label: or after this many sessions
    profit: float = 1.5  # barrier label: target, in daily volatilities
    stop: float = 2.0  # barrier label: stop
    horizon: int = 10  # barrier label: sessions
    model: str = "gbm"
    threshold: float = 0.55  # take a trade when the model gives it at least this
    folds: int = 5
    embargo: float = 0.01


def rsi2_events(closes: pd.DataFrame, entry: float = 10.0, trend: int = 200) -> pd.DataFrame:
    """Every RSI(2) signal: a close above the 200-day average with RSI(2) under ``entry``."""
    rsi = wilder_rsi(closes, 2)
    above = closes > closes.rolling(trend, min_periods=trend).mean()
    hits = (rsi < entry) & above
    stacked = hits.stack()  # noqa: PD013
    events = stacked[stacked.to_numpy(dtype=bool)].index.to_frame(index=False)
    events.columns = ["date", "symbol"]
    events["side"] = 1
    return events[["symbol", "date", "side"]].sort_values(["date", "symbol"]).reset_index(drop=True)


def label_events(
    events: pd.DataFrame, bars: dict[str, pd.DataFrame], cfg: MetaConfig
) -> pd.DataFrame:
    if cfg.label == "trade":
        close = bars["close"]
        bounce = close > close.rolling(cfg.exit_ma, min_periods=cfg.exit_ma).mean()
        return rule_labels(events, bars["open"], close, bounce, max_hold=cfg.max_hold)
    if cfg.label == "barrier":
        return triple_barrier(
            events, bars["open"], bars["high"], bars["low"], bars["close"],
            profit=cfg.profit, stop=cfg.stop, horizon=cfg.horizon,
        )  # fmt: skip
    raise ValueError(f"unknown label {cfg.label!r}: {', '.join(LABELS)}")


def dataset(
    events: pd.DataFrame,
    features: pd.DataFrame,
    bars: dict[str, pd.DataFrame],
    cfg: MetaConfig,
    columns: tuple[str, ...] = PRICE_FEATURES,
) -> pd.DataFrame:
    """Labelled events with the features of their signal day, sample weights (uniqueness) and
    the label's span (``start``, ``end``) for purging."""
    labelled = label_events(events, bars, cfg)
    if labelled.empty:
        return labelled
    keys = pd.MultiIndex.from_arrays(
        [labelled["date"], labelled["symbol"]], names=["date", "symbol"]
    )
    x = features.reindex(keys)[list(columns)].reset_index(drop=True)
    out = pd.concat([labelled.reset_index(drop=True), x], axis=1)
    out["weight"] = uniqueness(out["start"], out["end"], pd.DatetimeIndex(bars["close"].index))
    return out


def bet_size(p: Any) -> Any:
    """How much of a full position a probability earns (López de Prado, ch. 10): nothing at 0.5,
    rising with confidence; 2N(z) - 1 with z = (p - 0.5) / sqrt(p (1 - p))."""
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    z = (p - 0.5) / np.sqrt(p * (1 - p))
    normal = 0.5 * (1 + np.vectorize(math.erf)(z / math.sqrt(2)))
    return np.clip(2 * normal - 1, 0.0, 1.0)


def usable_columns(data: pd.DataFrame, columns: tuple[str, ...]) -> tuple[str, ...]:
    """The columns with something to learn from: present, and not all one value or missing
    (no macro data in the lake leaves its columns empty)."""

    def varies(c: str) -> bool:
        values = data[c].dropna().to_numpy()
        return len(values) > 0 and bool((values != values[0]).any())

    return tuple(c for c in columns if c in data.columns and varies(c))


def evaluate(
    data: pd.DataFrame, cfg: MetaConfig, columns: tuple[str, ...] = PRICE_FEATURES
) -> dict[str, Any]:
    """Out-of-fold probabilities (purged k-fold and walk-forward) and what filtering by them
    would have done to the primary strategy's trades."""
    used = usable_columns(data, columns)
    x, y, w = data[list(used)], data["label"].astype(int), data["weight"]
    factory = make_model(cfg.model)
    kfold = PurgedKFold(cfg.folds, cfg.embargo).split(data["start"], data["end"])
    p_cv = oof_predict(factory, x, y, kfold, w)
    wf = walk_forward(data["start"], data["end"], n_splits=cfg.folds, embargo=cfg.embargo)
    p_wf = oof_predict(factory, x, y, wf, w)
    report: dict[str, Any] = {
        "config": asdict(cfg),
        "columns": list(used),
        "events": len(data),
        "base_rate": float(y.mean()),
        "avg_trade": float(data["ret"].mean()),
        "first": str(pd.Timestamp(data["date"].min()).date()),
        "last": str(pd.Timestamp(data["date"].max()).date()),
    }
    for name, p in (("cv", p_cv), ("walk_forward", p_wf)):
        report[name] = _scores(p, y, data["ret"], cfg.threshold)
    return report | {"_probabilities": p_cv, "_walk_forward": p_wf}


def _scores(p: pd.Series, y: pd.Series, ret: pd.Series, threshold: float) -> dict[str, Any]:
    ok = p.notna()
    if ok.sum() < 20 or y[ok].nunique() < 2:
        return {"scored": int(ok.sum())}
    pp, yy, rr = p[ok], y[ok], ret[ok]
    keep = pp >= threshold
    sized = bet_size(pp)
    return {
        "scored": int(ok.sum()),
        "auc": float(roc_auc_score(yy, pp)),
        "log_loss": float(log_loss(yy, pp.clip(1e-6, 1 - 1e-6))),
        "brier": float(brier_score_loss(yy, pp)),
        "kept": int(keep.sum()),
        "kept_share": float(keep.mean()),
        "precision_all": float(yy.mean()),
        "precision_kept": float(yy[keep].mean()) if keep.any() else None,
        "avg_trade_all": float(rr.mean()),
        "avg_trade_kept": float(rr[keep].mean()) if keep.any() else None,
        "avg_trade_sized": float((rr * sized).sum() / sized.sum()) if sized.sum() > 0 else None,
    }


@dataclass(frozen=True)
class MetaModel:
    """A fitted filter: the estimator, the columns it reads, and what it was trained on."""

    estimator: Any
    columns: tuple[str, ...]
    events: int
    base_rate: float
    trained_to: pd.Timestamp  # the last labelled trade ended on this session

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """Probability that each row's trade works."""
        proba = self.estimator.predict_proba(features[list(self.columns)])
        return np.asarray(proba[:, 1], dtype=float)


def fit(
    data: pd.DataFrame, cfg: MetaConfig, columns: tuple[str, ...] = PRICE_FEATURES
) -> MetaModel:
    """A model trained on every labelled event in ``data`` (for live use: only events whose
    trades had ended before the day it's used). Raises when ``data`` has one class only."""
    if data["label"].nunique() < 2:
        raise ValueError("every event has the same label: nothing to learn")
    used = usable_columns(data, columns)
    if not used:
        raise ValueError("no usable feature columns")
    estimator = make_model(cfg.model)()
    kw = {weight_arg(estimator): data["weight"].to_numpy()}
    estimator.fit(data[list(used)], data["label"].astype(int), **kw)
    return MetaModel(
        estimator=estimator,
        columns=used,
        events=len(data),
        base_rate=float(data["label"].mean()),
        trained_to=pd.Timestamp(data["end"].max()),
    )


def train(
    bars: dict[str, pd.DataFrame],
    cfg: MetaConfig,
    *,
    sectors: dict[str, str] | None = None,
    min_events: int = 200,
    columns: tuple[str, ...] = PRICE_FEATURES,
) -> MetaModel | None:
    """Features, RSI(2) signals, labels and a fitted model from ``bars`` (wide OHLCV). Only
    trades that ended inside ``bars`` are labelled, so a model trained on the bars up to a day
    has seen nothing after it. None with fewer than ``min_events`` labelled signals."""
    events = rsi2_events(bars["close"], cfg.entry, cfg.trend)
    if len(events) < min_events:
        return None
    features = compute_features(bars, sectors=sectors)
    data = dataset(events, features, bars, cfg, columns)
    if len(data) < min_events or data["label"].nunique() < 2:
        return None
    return fit(data, cfg, columns)


def report_text(report: dict[str, Any]) -> str:
    """The evaluation in plain words."""
    won = "made money" if report["config"]["label"] == "trade" else "reached the target first"
    lines = [
        f"{report['events']} RSI(2) signals {report['first']} to {report['last']}; "
        f"{report['base_rate']:.0%} {won}, average trade {report['avg_trade']:+.2%}.",
    ]
    folds = report["config"]["folds"]
    for name, label in (("cv", f"Purged {folds}-fold"), ("walk_forward", "Walk-forward")):
        s = report.get(name) or {}
        if "auc" not in s:
            lines.append(f"{label}: too few scored events ({s.get('scored', 0)}).")
            continue
        kept = s["precision_kept"]
        lines.append(
            f"{label}: AUC {s['auc']:.3f} (0.5 is a coin flip). Keeping trades at "
            f"p >= {report['config']['threshold']:.2f}: {s['kept']} of {s['scored']} "
            f"({s['kept_share']:.0%}), hit rate {kept:.0%} vs {s['precision_all']:.0%}, "
            f"average trade {s['avg_trade_kept']:+.2%} vs {s['avg_trade_all']:+.2%}"
            if kept is not None
            else f"{label}: AUC {s['auc']:.3f}; no trade reached the threshold."
        )
    return "\n".join(lines)
