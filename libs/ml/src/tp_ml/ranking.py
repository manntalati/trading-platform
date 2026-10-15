"""Cross-sectional ranking: which stocks will beat the others over the next month.

At each month end every stock in the universe is described by its point-in-time features, each
turned into its rank among that day's stocks (so a model learns about *relative* standing, and
a market-wide move changes nothing). The target is the stock's rank on the return from the next
open to the open ``horizon`` sessions later. A model that orders the stocks well is worth
something even if it is useless at predicting the market.

It is measured the way the factor literature measures signals:

- **Information coefficient (IC)**: the correlation, across stocks on one date, between the
  score and the return that followed; **rank IC** the same on ranks. Averaged over dates, with
  its t-statistic and the share of dates it was positive. A mean rank IC of 0.03 to 0.05 is a
  good equity signal; 0.1 on large caps is too good to be true.
- **Buckets**: the stocks split into ``buckets`` groups by score each date; the average next
  return of each, and the top-minus-bottom spread (its Sharpe, annualized, from monthly
  rebalancing). Few stocks per bucket on a small universe: noisy, read it with the IC.
- **Turnover** of the top bucket: the share of it that changes each rebalance.

Every score is out of sample (walk-forward by default: trained only on labels that had ended
before the test month). The **baseline** is the classic 12-1 momentum score; a model has to
beat it to be worth its complexity.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from tp_ml.cv import PurgedKFold, oof_regress, walk_forward
from tp_ml.features import compute_features
from tp_ml.models import make_regressor, usable_columns

RANK_FEATURES = (
    "ret_1", "ret_5", "ret_21", "ret_63", "ret_126", "ret_252", "rel_sector_63",
    "vol_21", "vol_63", "vol_ratio", "atr_pct", "range_pct", "volume_z", "dollar_volume",
    "dist_ma20", "dist_ma50", "dist_ma200", "rsi_2", "rsi_14",
)  # fmt: skip
MONTHS_PER_YEAR = 12


@dataclass(frozen=True)
class RankConfig:
    model: str = "ridge"  # or "gbm"
    horizon: int = 21  # sessions held: about a month
    folds: int = 5
    min_train: float = 0.4  # walk-forward: the first 40% of dates only train
    buckets: int = 5
    embargo: float = 0.01


def rebalance_dates(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """The last session of each month."""
    days = pd.Series(index, index=index)
    return pd.DatetimeIndex(days.groupby([index.year, index.month]).max().to_numpy())


def cross_rank(panel: pd.DataFrame) -> pd.DataFrame:
    """Each column ranked among the stocks of the same date, scaled to -0.5..0.5 (missing stays
    missing). ``panel`` is indexed by (date, symbol)."""
    ranked = panel.groupby(level="date").rank(pct=True) - 0.5
    return ranked.reindex(panel.index)


def momentum_score(panel: pd.DataFrame) -> pd.Series:
    """12-1 momentum from the feature panel: the 252-day return less the latest month's."""
    return panel["ret_252"] - panel["ret_21"]


def ranking_dataset(
    bars: Mapping[str, pd.DataFrame],
    features: pd.DataFrame,
    cfg: RankConfig,
    columns: tuple[str, ...] = RANK_FEATURES,
) -> pd.DataFrame:
    """One row per (month end, stock) whose next ``horizon`` sessions are in ``bars``: the
    cross-sectionally ranked features, the 12-1 momentum baseline, the forward return
    (``fwd``), its rank (``target``) and the label's span (``start``, ``end``) for purging."""
    opens = bars["open"].sort_index()
    index = pd.DatetimeIndex(opens.index)
    entry, exit_ = opens.shift(-1), opens.shift(-(1 + cfg.horizon))
    fwd = exit_ / entry - 1
    dates = rebalance_dates(index)
    dates = dates[index.get_indexer(dates) + 1 + cfg.horizon < len(index)]
    if len(dates) == 0:
        return pd.DataFrame()
    at = features[features.index.get_level_values("date").isin(dates)]
    out = cross_rank(at[list(columns)])
    out["momentum"] = momentum_score(at)
    stacked = fwd.loc[dates].stack()  # noqa: PD013
    stacked.index.names = ["date", "symbol"]
    out["fwd"] = stacked.reindex(out.index)
    out = out[out["fwd"].notna() & out[list(columns)].notna().any(axis=1)]
    out["target"] = out.groupby(level="date")["fwd"].rank(pct=True) - 0.5
    positions = index.get_indexer(out.index.get_level_values("date"))
    out["start"] = index[positions + 1]
    out["end"] = index[positions + 1 + cfg.horizon]
    # dates with too few stocks to rank say nothing
    counts = out.groupby(level="date")["fwd"].transform("size")
    return out[counts >= max(cfg.buckets, 5)]


def _per_date(frame: pd.DataFrame, score: str, buckets: int) -> pd.DataFrame:
    rows = []
    for day, g in frame.dropna(subset=[score]).groupby(level="date"):
        if len(g) < max(buckets, 5) or g[score].nunique() < 2:
            continue
        s, r = g[score], g["fwd"]
        bucket = pd.qcut(s.rank(method="first"), buckets, labels=False)
        means = r.groupby(bucket).mean().reindex(range(buckets)).to_numpy(dtype=float)
        rows.append(
            {
                "date": day,
                "ic": float(np.corrcoef(s, r)[0, 1]),
                "rank_ic": float(s.rank().corr(r.rank())),
                "spread": float(means[-1] - means[0]),
                **{f"b{k + 1}": float(v) for k, v in enumerate(means)},
                "top": frozenset(g.index.get_level_values("symbol")[bucket == buckets - 1]),
            }
        )
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()


def signal_report(frame: pd.DataFrame, score: str, buckets: int) -> dict[str, Any]:
    """IC, rank IC, bucket returns, spread and turnover of ``score`` against ``fwd``."""
    daily = _per_date(frame, score, buckets)
    if daily.empty:
        return {"dates": 0}
    n = len(daily)
    ic, rank_ic, spread = daily["ic"], daily["rank_ic"], daily["spread"]
    tops = daily["top"].tolist()
    changes = [1 - len(a & b) / max(len(b), 1) for a, b in itertools.pairwise(tops)]
    spread_sd = float(spread.std(ddof=1)) if n > 1 else math.nan
    return {
        "dates": n,
        "first": str(daily.index.min().date()),
        "last": str(daily.index.max().date()),
        "ic": float(ic.mean()),
        "rank_ic": float(rank_ic.mean()),
        "rank_ic_t": _t_stat(rank_ic),
        "rank_ic_positive": float((rank_ic > 0).mean()),
        "buckets": [float(daily[f"b{k}"].mean()) for k in range(1, buckets + 1)],
        "spread": float(spread.mean()),
        "spread_sharpe": float(spread.mean() / spread_sd * math.sqrt(MONTHS_PER_YEAR))
        if spread_sd and spread_sd > 0
        else None,
        "top_turnover": float(np.mean(changes)) if changes else None,
    }


def evaluate_ranking(
    data: pd.DataFrame, cfg: RankConfig, columns: tuple[str, ...] = RANK_FEATURES
) -> dict[str, Any]:
    """Out-of-sample scores (walk-forward and purged k-fold) and their reports, next to the
    12-1 momentum baseline over the same dates."""
    columns = usable_columns(data, columns)
    x, y = data[list(columns)], data["target"]
    factory = make_regressor(cfg.model)
    wf = walk_forward(data["start"], data["end"], n_splits=cfg.folds, min_train=cfg.min_train,
                      embargo=cfg.embargo)  # fmt: skip
    kfold = PurgedKFold(cfg.folds, cfg.embargo).split(data["start"], data["end"])
    scored = data.assign(
        p_walk_forward=oof_regress(factory, x, y, wf),
        p_cv=oof_regress(factory, x, y, kfold),
    )
    tested = scored[scored["p_walk_forward"].notna()]
    return {
        "config": asdict(cfg),
        "columns": list(columns),
        "rows": len(data),
        "symbols": int(data.index.get_level_values("symbol").nunique()),
        "walk_forward": signal_report(tested, "p_walk_forward", cfg.buckets),
        "cv": signal_report(scored, "p_cv", cfg.buckets),
        # the baseline over the walk-forward's dates, so the two are comparable
        "momentum": signal_report(tested, "momentum", cfg.buckets),
        "_scored": scored,
    }


@dataclass(frozen=True)
class RankModel:
    estimator: Any
    columns: tuple[str, ...]
    dates: int  # month ends trained on
    trained_to: pd.Timestamp  # the last label ended on this session

    def score(self, latest: pd.DataFrame) -> pd.Series:
        """Scores for one day's stocks (``latest``: one row per symbol, raw features); higher
        is better. The features are ranked across these stocks first, as in training."""
        ranked = latest[list(self.columns)].rank(pct=True) - 0.5
        return pd.Series(self.estimator.predict(ranked), index=latest.index, dtype=float)


def fit_ranker(
    data: pd.DataFrame, cfg: RankConfig, columns: tuple[str, ...] = RANK_FEATURES
) -> RankModel:
    columns = usable_columns(data, columns)
    if not columns:
        raise ValueError("no usable feature columns")
    estimator = make_regressor(cfg.model)()
    estimator.fit(data[list(columns)], data["target"])
    return RankModel(
        estimator=estimator,
        columns=columns,
        dates=int(data.index.get_level_values("date").nunique()),
        trained_to=pd.Timestamp(data["end"].max()),
    )


def train_ranker(
    bars: Mapping[str, pd.DataFrame],
    cfg: RankConfig,
    *,
    sectors: Mapping[str, str] | None = None,
    min_dates: int = 24,
    columns: tuple[str, ...] = RANK_FEATURES,
) -> RankModel | None:
    """A ranker trained on every month end in ``bars`` whose label had ended inside them; None
    with fewer than ``min_dates`` month ends."""
    data = ranking_dataset(bars, compute_features(bars, sectors=sectors), cfg, columns)
    if data.empty or data.index.get_level_values("date").nunique() < min_dates:
        return None
    return fit_ranker(data, cfg, columns)


def report_text(report: dict[str, Any]) -> str:
    cfg = report["config"]
    lines = [
        f"{report['symbols']} stocks, {report['rows']} stock-months; target: the next "
        f"{cfg['horizon']} sessions' return, ranked. Model: {cfg['model']}.",
        "",
        f"{'':26}{'rank IC':>9}{'t-stat':>8}{'IC>0':>7}{'spread/mo':>11}{'Sharpe':>8}"
        f"{'top turnover':>14}",
    ]
    for key, label in (
        ("walk_forward", "Model, walk-forward"),
        ("cv", "Model, purged k-fold"),
        ("momentum", "12-1 momentum (baseline)"),
    ):
        s = report.get(key) or {}
        if not s.get("dates"):
            lines.append(f"{label:26}  too few dates")
            continue
        lines.append(
            f"{label:26}{s['rank_ic']:>+9.3f}{_num(s.get('rank_ic_t'), '+.1f'):>8}"
            f"{s['rank_ic_positive']:>7.0%}{s['spread']:>+11.2%}"
            f"{_num(s.get('spread_sharpe'), '.2f'):>8}{_num(s.get('top_turnover'), '.0%'):>14}"
        )
    wf = report.get("walk_forward") or {}
    if wf.get("dates"):
        returns = "  ".join(f"{r:+.2%}" for r in wf["buckets"])
        lines += [
            "",
            f"Walk-forward, {wf['dates']} month ends {wf['first']} to {wf['last']}: average "
            f"next-month return by bucket, worst to best score: {returns}",
        ]
    return "\n".join(lines)


def _t_stat(values: pd.Series) -> float | None:
    sd = float(values.std(ddof=1)) if len(values) > 2 else 0.0
    return float(values.mean() / sd * math.sqrt(len(values))) if sd > 0 else None


def _num(value: Any, fmt: str) -> str:
    return (
        "—"
        if value is None or (isinstance(value, float) and math.isnan(value))
        else (format(value, fmt))
    )
