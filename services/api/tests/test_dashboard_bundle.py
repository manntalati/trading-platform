"""The served dashboard is rebuilt when its source is newer than the bundle."""

import logging
import os
from pathlib import Path

import pytest

from tp_api.dashboard import bundle_state, ensure_built


def touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_a_bundle_older_than_its_source_is_stale(tmp_path: Path) -> None:
    assert bundle_state(tmp_path).stale  # never built
    touch(tmp_path / "src" / "pages" / "Trades.tsx", 1_000)
    touch(tmp_path / "dist" / "index.html", 2_000)
    assert not bundle_state(tmp_path).stale
    touch(tmp_path / "src" / "pages" / "Trades.tsx", 3_000)  # pulled new code
    state = bundle_state(tmp_path)
    assert state.stale
    assert state.to_dict()["stale"] is True


def test_without_node_it_says_how_to_build(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(tmp_path / "package.json", 3_000)
    touch(tmp_path / "dist" / "index.html", 2_000)
    monkeypatch.setattr("tp_api.dashboard.shutil.which", lambda name: None)
    with caplog.at_level(logging.WARNING):
        assert ensure_built(tmp_path).stale
    assert "make web-install web-build" in caplog.text


def test_it_rebuilds_with_npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    touch(tmp_path / "package.json", 3_000)
    (tmp_path / "node_modules").mkdir()
    calls: list[list[str]] = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def run(cmd: list[str], **kwargs: object) -> Done:
        calls.append(cmd)
        touch(tmp_path / "dist" / "index.html", 4_000)
        return Done()

    monkeypatch.setattr("tp_api.dashboard.shutil.which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr("tp_api.dashboard.subprocess.run", run)
    assert not ensure_built(tmp_path).stale
    assert calls == [["/usr/bin/npm", "run", "build"]]
