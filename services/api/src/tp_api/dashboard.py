"""The built dashboard (``apps/dashboard/dist``) and whether it's older than its source.

``tp-api`` serves whatever was last built. After pulling new dashboard code that bundle is out
of date: new pages (Paper, Trades) simply aren't there. So the server checks on start and, when
Node is installed, rebuilds it first.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parents[4] / "apps" / "dashboard"
SOURCES = ("src", "public", "index.html", "package.json", "package-lock.json", "vite.config.ts")


@dataclass(frozen=True)
class BundleState:
    built_at: datetime | None  # None: never built
    source_changed_at: datetime | None

    @property
    def stale(self) -> bool:
        if self.built_at is None:
            return True
        return self.source_changed_at is not None and self.source_changed_at > self.built_at

    def to_dict(self) -> dict[str, object]:
        return {
            "built_at": self.built_at.isoformat() if self.built_at else None,
            "source_changed_at": (
                self.source_changed_at.isoformat() if self.source_changed_at else None
            ),
            "stale": self.stale,
        }


def bundle_state(app_dir: Path = APP_DIR) -> BundleState:
    index = app_dir / "dist" / "index.html"
    built = _mtime(index) if index.exists() else None
    newest: float | None = None
    for name in SOURCES:
        path = app_dir / name
        files = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]
        for f in files:
            if f.exists():
                newest = max(newest or 0.0, f.stat().st_mtime)
    return BundleState(built, _stamp(newest) if newest is not None else None)


def ensure_built(app_dir: Path = APP_DIR) -> BundleState:
    """Rebuild the dashboard if its bundle is missing or older than its source. Without Node or
    its packages, say how to build it instead of failing: the API works either way."""
    state = bundle_state(app_dir)
    if not state.stale or not (app_dir / "package.json").exists():
        return state
    npm = shutil.which("npm")
    if npm is None or not (app_dir / "node_modules").exists():
        log.warning(
            "the dashboard bundle is %s; build it with `make web-install web-build` (needs Node "
            "22+), or new pages won't show",
            "missing" if state.built_at is None else "older than its source",
        )
        return state
    log.info("the dashboard changed since it was built; rebuilding (npm run build)")
    done = subprocess.run(  # a fixed command, no user input
        [npm, "run", "build"], cwd=app_dir, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        log.error("dashboard build failed:\n%s", (done.stdout + done.stderr)[-4000:])
    return bundle_state(app_dir)


def _mtime(path: Path) -> datetime:
    return _stamp(path.stat().st_mtime)


def _stamp(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)
