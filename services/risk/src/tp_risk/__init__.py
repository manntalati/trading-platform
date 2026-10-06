"""Pre-trade risk checks that every order passes and no strategy can see or change.

Limits come from ``config/risk.toml`` (the project plan's starting values). State that must
survive restarts (kill switch, disabled strategies, equity peaks) lives in
``<data root>/state/risk.json``.
"""

__version__ = "0.1.0"
