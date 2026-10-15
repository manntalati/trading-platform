from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from tp_ml.cv import PurgedKFold, oof_predict, uniqueness, walk_forward, weight_arg


def spans(n: int = 100, hold: int = 5) -> tuple[pd.Series, pd.Series]:
    """One label a day, each spanning ``hold`` sessions: neighbours overlap."""
    start = pd.Series(pd.bdate_range("2024-01-01", periods=n))
    end = start + pd.offsets.BDay(hold - 1)
    return start, end


def test_purged_folds_cover_every_sample_once_and_never_overlap_training() -> None:
    start, end = spans()
    tested: list[int] = []
    for train, test in PurgedKFold(5, embargo=0.0).split(start, end):
        tested += test.tolist()
        t0, t1 = start.iloc[test].min(), end.iloc[test].max()
        overlapping = (start.iloc[train] <= t1) & (end.iloc[train] >= t0)
        assert not overlapping.any()
        assert len(train) > 0
    assert sorted(tested) == list(range(100))


def test_purging_removes_the_neighbours_plain_kfold_would_keep() -> None:
    start, end = spans(hold=5)
    folds = list(PurgedKFold(5, embargo=0.0).split(start, end))
    train, test = folds[2]  # a middle fold: samples 40..59
    assert test.tolist() == list(range(40, 60))
    # the test labels span day 40 to day 63 (sample 59 ends 4 sessions after it starts): labels
    # ending inside that (36..39) or starting inside it (60..63) are purged
    assert not set(range(36, 64)) & set(train.tolist())
    assert {35, 64} <= set(train.tolist())


def test_embargo_drops_samples_just_after_the_test_block() -> None:
    start, end = spans(hold=1)
    plain = dict(enumerate(PurgedKFold(5, embargo=0.0).split(start, end)))
    embargoed = dict(enumerate(PurgedKFold(5, embargo=0.05).split(start, end)))
    after = set(plain[1][0].tolist()) - set(embargoed[1][0].tolist())
    assert after
    assert min(after) >= 40  # only samples after the test block (20..39) go
    assert set(plain[1][0][plain[1][0] < 20]) == set(embargoed[1][0][embargoed[1][0] < 20])


def test_too_few_samples_raise() -> None:
    start, end = spans(3)
    with pytest.raises(ValueError, match="folds"):
        list(PurgedKFold(5).split(start, end))


def test_walk_forward_trains_only_on_finished_past_labels() -> None:
    start, end = spans(200, hold=10)
    splits = list(walk_forward(start, end, n_splits=4, min_train=0.4, embargo=0.0))
    assert len(splits) == 4
    first_tested = min(int(test.min()) for _, test in splits)
    assert first_tested == 80
    for train, test in splits:
        assert end.iloc[train].max() < start.iloc[test].min()


def test_uniqueness_weights_overlapping_labels_down() -> None:
    days = pd.bdate_range("2024-01-01", periods=10)
    start = pd.Series([days[0], days[0], days[5]])
    end = pd.Series([days[2], days[2], days[6]])
    w = uniqueness(start, end, days)
    assert w.tolist() == pytest.approx([0.5, 0.5, 1.0])


def test_weight_argument_reaches_a_pipelines_last_step() -> None:
    assert weight_arg(LogisticRegression()) == "sample_weight"
    pipe = make_pipeline(StandardScaler(), LogisticRegression())
    assert weight_arg(pipe) == "logisticregression__sample_weight"


def test_oof_predictions_come_from_models_that_never_saw_the_sample() -> None:
    rng = np.random.default_rng(0)
    n = 300
    start, end = spans(n, hold=3)
    x = pd.DataFrame({"a": rng.normal(size=n), "b": rng.normal(size=n)})
    y = pd.Series((x["a"] + 0.3 * rng.normal(size=n) > 0).astype(int))
    w = pd.Series(np.ones(n))
    folds = PurgedKFold(5, 0.01).split(start, end)
    p = oof_predict(lambda: LogisticRegression(), x, y, folds, w)
    assert p.notna().all()
    assert ((p > 0.5) == (y == 1)).mean() > 0.8
    # walk-forward leaves the first block (never tested) empty
    wf = oof_predict(lambda: LogisticRegression(), x, y, walk_forward(start, end, n_splits=3), w)
    assert wf.iloc[:100].isna().all()
    assert wf.iloc[150:].notna().all()


def test_single_class_folds_are_skipped() -> None:
    start, end = spans(50)
    x = pd.DataFrame({"a": np.arange(50.0)})
    y = pd.Series([0] * 50)
    p = oof_predict(lambda: LogisticRegression(), x, y, PurgedKFold(5).split(start, end))
    assert p.isna().all()
