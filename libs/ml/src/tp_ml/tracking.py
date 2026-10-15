"""Experiment tracking with MLflow, in the data root.

Every model run records its parameters, metrics, the feature set version it used, the git commit
and the data version, plus any artifacts (a report, the predictions). The store is a SQLite file
(``<data root>/mlflow/mlflow.db``) with artifacts beside it, so nothing needs a server; browse it
with ``tp-ml ui`` (MLflow's own web UI on http://127.0.0.1:5000).
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import mlflow

from tp_core.storage import Lake
from tp_core.versions import git_version


def tracking_dir(lake: Lake) -> Path:
    return (lake.root / "mlflow").resolve()


def tracking_uri(lake: Lake) -> str:
    return f"sqlite:///{tracking_dir(lake) / 'mlflow.db'}"


def _experiment(lake: Lake, name: str) -> str:
    mlflow.set_tracking_uri(tracking_uri(lake))
    found = mlflow.get_experiment_by_name(name)
    if found is not None:
        return str(found.experiment_id)
    artifacts = tracking_dir(lake) / "artifacts" / name
    artifacts.mkdir(parents=True, exist_ok=True)
    return str(mlflow.create_experiment(name, artifact_location=artifacts.as_uri()))


@contextmanager
def run(
    lake: Lake,
    experiment: str,
    *,
    name: str,
    params: Mapping[str, Any],
    tags: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    """An MLflow run with the parameters and the code version already logged."""
    tracking_dir(lake).mkdir(parents=True, exist_ok=True)
    experiment_id = _experiment(lake, experiment)
    code = git_version()
    with mlflow.start_run(experiment_id=experiment_id, run_name=name) as active:
        mlflow.log_params({k: str(v)[:500] for k, v in params.items()})
        mlflow.set_tags(
            {"git_commit": code.get("commit"), "git_dirty": code.get("dirty"), **(tags or {})}
        )
        yield active


def log_metrics(metrics: Mapping[str, float | None]) -> None:
    mlflow.log_metrics({k: float(v) for k, v in metrics.items() if v is not None and v == v})


def log_text(text: str, name: str) -> None:
    mlflow.log_text(text, name)


def runs(lake: Lake, experiment: str) -> Any:
    """The experiment's runs as a DataFrame (newest first)."""
    mlflow.set_tracking_uri(tracking_uri(lake))
    return mlflow.search_runs(experiment_names=[experiment], order_by=["start_time DESC"])
