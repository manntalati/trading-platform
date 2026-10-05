from pathlib import Path

import pytest

from tp_research.notebook import lake, repo_root

REPO = Path(__file__).resolve().parents[2]


def test_repo_root_found_from_a_subdirectory() -> None:
    assert repo_root(REPO / "research" / "notebooks") == REPO


def test_lake_defaults_to_repo_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TP_DATA_ROOT", raising=False)
    monkeypatch.chdir(REPO / "research")
    assert lake().root == REPO / "data"


def test_lake_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TP_DATA_ROOT", str(tmp_path))
    assert lake().root == tmp_path
