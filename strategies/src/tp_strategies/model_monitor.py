"""The machine-learning strategies' models in use: retrained on schedule, registered, and watched
(``tp_ml.monitor``).

Each day (``tp-models monitor``, run by the job scheduler after the evening bars), for strategy 13's
ranker and strategy 12's meta-labeling filter, with the parameters they trade with in
``config/paper.toml`` (else their defaults):

1. **Retrain on schedule.** When the strategy's retrain date has moved on (every ``retrain``
   months) or its parameters changed, train a new version on the bars before that date, exactly
   as the strategy does, score it out of sample (walk-forward), profile its training inputs, and
   register it.
2. **Predict** with the registered version and keep the scores: the ranker scores every stock,
   the filter every RSI(2) signal of the day.
3. **Drift**: each input's last month against its training distribution (PSI).
4. **Live accuracy**: the kept predictions against what happened since.

The strategies themselves retrain from the same bars on the same dates, so the registered model is
the one they trade with; the registry is the record of it.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

import pandas as pd

from tp_core.bars import load_bars, missing_symbols
from tp_core.storage import Lake
from tp_ml import monitor
from tp_ml.cv import retrain_date
from tp_ml.features import LOOKBACK, compute_features
from tp_ml.meta import dataset, evaluate, label_events, rsi2_events
from tp_ml.meta import fit as fit_meta
from tp_ml.ranking import RankConfig, evaluate_ranking, fit_ranker, ranking_dataset
from tp_ml.store import wide_bars
from tp_strategies.library import build
from tp_strategies.library.ml_rank import MlRank
from tp_strategies.library.rsi2_meta import Rsi2Meta

log = logging.getLogger(__name__)

MONITORED = ("ml-rank", "rsi2-meta")
TRAIN_WINDOW = 252  # sessions before the cutoff the drift profile comes from
RECENT = 21  # sessions compared with it
MIN_LIVE_DATES = 6
MIN_LIVE_PREDICTIONS = 20


def _bars(lake: Lake, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """The strategy's own symbols that have bars: what it trains and trades on."""
    wanted = list(dict.fromkeys(symbols))
    present = [s for s in wanted if s not in set(missing_symbols(lake, wanted))]
    return wide_bars(load_bars(lake, present)) if present else {}


def _signature(params: dict[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, default=str)


def _train(
    strategy: MlRank | Rsi2Meta, past: dict[str, pd.DataFrame], sectors: dict[str, str]
) -> tuple[Any, pd.DataFrame, dict[str, Any]] | None:
    """(model, its training features, out-of-sample validation) as the strategy would train it."""
    feats = compute_features(past, sectors=sectors)
    if isinstance(strategy, MlRank):
        cfg = RankConfig(model=strategy.model, horizon=strategy.horizon)
        data = ranking_dataset(past, feats, cfg)
        if data.empty or data.index.get_level_values("date").nunique() < strategy.min_dates:
            return None
        ranker = fit_ranker(data, cfg)
        wf = evaluate_ranking(data, cfg)["walk_forward"]
        keys = ("dates", "rank_ic", "rank_ic_t", "rank_ic_positive", "spread")
        return ranker, feats, {k: wf.get(k) for k in keys}
    cfg_meta = strategy.config()
    events = rsi2_events(past["close"], strategy.entry, strategy.trend)
    data = dataset(events, feats, past, cfg_meta)
    if len(data) < strategy.min_events or data["label"].nunique() < 2:
        return None
    meta = fit_meta(data, cfg_meta)
    scores = evaluate(data, cfg_meta)["walk_forward"]
    wanted = ("scored", "auc", "brier", "precision_all", "precision_kept", "kept_share")
    return meta, feats, {k: scores.get(k) for k in wanted}


def monitor_model(
    lake: Lake,
    name: str,
    params: dict[str, Any] | None = None,
    *,
    sectors: dict[str, str] | None = None,
    now: datetime,
) -> dict[str, Any]:
    """Retrain if due, predict today, check drift and live accuracy; the report (saved too)."""
    strategy = build(name, params or {})
    if not isinstance(strategy, MlRank | Rsi2Meta):
        raise ValueError(f"{name} has no model to monitor: {', '.join(MONITORED)}")
    report: dict[str, Any] = {"model": name, "title": strategy.title, "checked_at": now.isoformat()}
    bars = _bars(lake, list(strategy.universe))
    if not bars or len(bars["close"].columns) < 10:
        report |= {
            "status": "skipped",
            "reasons": ["no bars for its universe: tp-data bars backfill"],
        }
        monitor.save_report(lake, name, report, now.date())
        return report
    close = bars["close"]
    last = pd.Timestamp(close.index[-1])
    today: date = last.date()
    report["as_of"] = today.isoformat()
    sectors = {s: v for s, v in (sectors or {}).items() if s in close.columns}
    cutoff = retrain_date(last, strategy.retrain)
    signature = _signature(strategy.params())

    version = monitor.latest(lake, name)
    stale = version is None or (
        version.get("cutoff") != cutoff.date().isoformat()
        or version.get("params_signature") != signature
    )
    report["retrained"] = False
    if stale:
        past = {k: v[v.index < cutoff] for k, v in bars.items()}
        trained = _train(strategy, past, sectors)
        if trained is not None:
            model, feats, validation = trained
            window = pd.DatetimeIndex(past["close"].index)[-TRAIN_WINDOW:]
            recent_train = feats[feats.index.get_level_values("date").isin(window)]
            version = monitor.register(
                lake,
                name,
                model,
                {
                    "cutoff": cutoff.date(),
                    "trained_at": now,
                    "trained_to": model.trained_to,
                    "params": strategy.params(),
                    "params_signature": signature,
                    "columns": list(model.columns),
                    "examples": getattr(model, "dates", None) or getattr(model, "events", None),
                    "validation": validation,
                    "profile": monitor.profile(recent_train, model.columns),
                },
            )
            report["retrained"] = True
            log.info("%s: registered %s", name, version["version"])
    if version is None:
        report |= {"status": "waiting",
                   "reasons": ["not enough history to train a model yet"]}  # fmt: skip
        monitor.save_report(lake, name, report, today)
        return report
    model = monitor.load_model(lake, name, version["version"])
    shown = ("version", "cutoff", "trained_at", "trained_to", "examples", "validation")
    report["version"] = {k: version.get(k) for k in shown}

    tail = {k: v.iloc[-(LOOKBACK + RECENT) :] for k, v in bars.items()}
    feats_now = compute_features(tail, sectors=sectors)
    dates = feats_now.index.get_level_values("date")
    latest = feats_now[dates == last].droplevel("date")
    if isinstance(strategy, MlRank):
        latest = latest[latest["ret_21"].notna()]
        scores = model.score(latest) if len(latest) else pd.Series(dtype=float)
    else:
        signals = rsi2_events(close.iloc[-(strategy.trend + 5) :], strategy.entry, strategy.trend)
        today_signals = [str(s) for s in signals.loc[signals["date"] == last, "symbol"]]
        scores = pd.Series(
            model.predict(latest.reindex(today_signals)) if today_signals else [],
            index=today_signals, dtype=float,
        )  # fmt: skip
    report["predictions_today"] = monitor.record_predictions(
        lake, name, version["version"], today, scores
    )

    recent = feats_now[dates.isin(pd.DatetimeIndex(close.index)[-RECENT:])]
    shifts = monitor.drift(version.get("profile") or {}, recent)
    report["drift"] = shifts[:10]
    preds = monitor.load_predictions(lake, name)
    reasons: list[str] = []
    shifted = [d["feature"] for d in shifts if d["level"] == "shifted"]
    if shifted:
        reasons.append(f"inputs unlike its training data: {', '.join(shifted[:5])}")
    if isinstance(strategy, MlRank):
        live = monitor.live_rank_ic(preds, close, strategy.horizon)
        if live["dates"] >= MIN_LIVE_DATES and (live["mean_ic"] or 0.0) <= 0:
            reasons.append(f"live rank IC {live['mean_ic']:+.3f} over {live['dates']} periods")
    else:
        live = _meta_live(preds, bars, strategy)
        hit, base = live.get("taken_hit_rate"), live.get("base_rate")
        if live.get("predictions", 0) >= MIN_LIVE_PREDICTIONS and hit is not None and hit < base:
            reasons.append(f"signals it takes win {hit:.0%}, below all signals' {base:.0%}")
    report |= {"live": live, "status": "warn" if reasons else "ok", "reasons": reasons}
    monitor.save_report(lake, name, report, today)
    return report


def _meta_live(
    preds: pd.DataFrame, bars: dict[str, pd.DataFrame], strategy: Rsi2Meta
) -> dict[str, Any]:
    """The filter's live predictions labelled by the trades RSI(2)'s exits made (once ended)."""
    if preds.empty:
        return {"predictions": 0}
    events = pd.DataFrame(
        {"symbol": preds["symbol"], "date": pd.DatetimeIndex(pd.to_datetime(preds["date"])),
         "side": 1}
    )  # fmt: skip
    labelled = label_events(events, bars, strategy.config())
    if labelled.empty:
        return {"predictions": 0, "waiting": len(preds)}
    scored = labelled.merge(
        preds.assign(date=pd.to_datetime(preds["date"]))[["symbol", "date", "score"]],
        on=["symbol", "date"],
    )
    out = monitor.live_classification(scored, strategy.threshold)
    return out | {"waiting": len(preds) - len(scored)}


def monitor_all(
    lake: Lake,
    names: list[str],
    params: dict[str, dict[str, Any]],
    *,
    sectors: dict[str, str],
    now: datetime,
) -> list[dict[str, Any]]:
    out = []
    for name in names:
        try:
            out.append(monitor_model(lake, name, params.get(name), sectors=sectors, now=now))
        except Exception as exc:  # one model failing must not hide the other's check
            log.exception("monitoring %s failed", name)
            report = {"model": name, "status": "error", "reasons": [f"{type(exc).__name__}: {exc}"],
                      "checked_at": now.isoformat()}  # fmt: skip
            monitor.save_report(lake, name, report, now.date())
            out.append(report)
    return out
