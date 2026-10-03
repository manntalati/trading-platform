"""Shared notebook helpers: locate the data lake, and one consistent plot style.

Matplotlib comes from the ``research`` dependency group, so it is imported lazily here: this
module stays importable (and type-checkable) without it.
"""

from __future__ import annotations

import os
from pathlib import Path

from tp_core.storage import Lake

# Categorical slots in fixed order (validated palette; never cycle or reorder).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
REFERENCE = "#8a8985"  # neutral gray for benchmarks, reference curves and zero lines
TEXT = "#52514e"


def repo_root(start: Path | None = None) -> Path:
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config" / "universes.toml").exists():
            return candidate
    raise FileNotFoundError("run from inside the trading-platform repo")


def lake() -> Lake:
    """The lake at $TP_DATA_ROOT, or <repo>/data."""
    root = os.environ.get("TP_DATA_ROOT")
    return Lake(Path(root) if root else repo_root() / "data")


def use_style() -> None:
    """Thin marks, recessive axes and grid, no chart junk."""
    import matplotlib as mpl
    from cycler import cycler

    mpl.rcParams.update(
        {
            "figure.figsize": (9, 4),
            "figure.dpi": 110,
            "axes.prop_cycle": cycler(color=SERIES),
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": "#c3c2b7",
            "axes.labelcolor": TEXT,
            "axes.titlesize": 12,
            "axes.grid": True,
            "grid.color": "#e6e5e0",
            "grid.linewidth": 0.8,
            "xtick.color": TEXT,
            "ytick.color": TEXT,
            "lines.linewidth": 1.6,
            "legend.frameon": False,
        }
    )
