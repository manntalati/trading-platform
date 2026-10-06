from pathlib import Path

import pytest

from tp_core.config import MissingCredentialsError, Settings, load_universes

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_env_aliases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("APCA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "hunter2")
    monkeypatch.setenv("TP_DATA_ROOT", "/srv/lake")
    settings = Settings()
    assert settings.require_alpaca_keys() == ("key", "hunter2")
    assert settings.data_root == Path("/srv/lake")
    assert settings.alpaca_paper is True
    assert "hunter2" not in repr(settings)  # SecretStr keeps keys out of logs


def test_missing_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for var in ("ALPACA_API_KEY", "APCA_API_KEY_ID", "ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingCredentialsError):
        Settings().require_alpaca_keys()


def test_repo_universe_has_14_etfs_and_38_stocks() -> None:
    universes = load_universes(REPO_ROOT / "config" / "universes.toml")
    assert len(universes.bars) == 52
    assert {"SPY", "EFA", "IEF", "VNQ", "DBC", "AGG", "BIL"} <= set(universes.bars)


def test_repo_universe_has_options_underlyings() -> None:
    universes = load_universes(REPO_ROOT / "config" / "universes.toml")
    assert 20 <= len(universes.options_underlyings) <= 30
    assert universes.options_max_dte == 365


def test_duplicate_symbols_rejected(tmp_path: Path) -> None:
    path = tmp_path / "u.toml"
    path.write_text('[bars]\netfs = ["SPY"]\nstocks = ["SPY"]\n')
    with pytest.raises(ValueError, match="duplicate"):
        load_universes(path)
