"""Runtime settings, read from environment variables or a git-ignored ``.env`` file.

Platform settings use the ``TP_`` prefix. Alpaca keys accept both this project's names
(``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``) and Alpaca's own SDK convention
(``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY``).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BarsFeed = Literal["sip", "iex", "delayed_sip"]
OptionsFeed = Literal["indicative", "opra"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    alpaca_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ALPACA_API_KEY", "APCA_API_KEY_ID")
    )
    alpaca_secret_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")
    )
    # Paper keys only until a strategy passes the promotion gates in the project plan.
    alpaca_paper: bool = Field(default=True, validation_alias="TP_ALPACA_PAPER")

    data_root: Path = Field(default=Path("data"), validation_alias="TP_DATA_ROOT")
    # Historical SIP (all US venues) is free on Alpaca's Basic plan once it is >15 min old,
    # which is always true for completed daily bars. IEX alone is a small slice of volume.
    bars_feed: BarsFeed = Field(default="sip", validation_alias="TP_BARS_FEED")
    # Free plan: options come from the `indicative` feed (derived, not OPRA) and real-time stock
    # quotes from IEX only. Paid plans can switch to `opra` / `sip`.
    options_feed: OptionsFeed = Field(default="indicative", validation_alias="TP_OPTIONS_FEED")
    quotes_feed: BarsFeed = Field(default="iex", validation_alias="TP_QUOTES_FEED")
    universes_file: Path = Field(
        default=Path("config/universes.toml"), validation_alias="TP_UNIVERSES_FILE"
    )
    classifications_file: Path = Field(
        default=Path("config/classifications.toml"), validation_alias="TP_CLASSIFICATIONS_FILE"
    )
    risk_file: Path = Field(default=Path("config/risk.toml"), validation_alias="TP_RISK_FILE")
    paper_file: Path = Field(default=Path("config/paper.toml"), validation_alias="TP_PAPER_FILE")
    # "alpaca": the Alpaca paper account (always the paper endpoint). "fake": simulated offline.
    paper_broker: Literal["alpaca", "fake"] = Field(
        default="alpaca", validation_alias="TP_PAPER_BROKER"
    )

    # If set, the dashboard API requires it (Authorization: Bearer <token>; ?token= on the
    # WebSocket). The API binds to localhost by default; set this before exposing it anywhere.
    dashboard_token: SecretStr | None = Field(default=None, validation_alias="TP_DASHBOARD_TOKEN")
    # Host names the dashboard answers to (DNS-rebinding protection). Add the name you reach it
    # by (e.g. a Tailscale machine name) when serving beyond localhost. Comma-separated.
    dashboard_hosts: str = Field(
        default="localhost,127.0.0.1", validation_alias="TP_DASHBOARD_HOSTS"
    )

    # SnapTrade personal API key (read-only link to Fidelity): https://dashboard.snaptrade.com
    snaptrade_client_id: str | None = Field(default=None, validation_alias="SNAPTRADE_CLIENT_ID")
    snaptrade_consumer_key: SecretStr | None = Field(
        default=None, validation_alias="SNAPTRADE_CONSUMER_KEY"
    )

    def require_alpaca_keys(self) -> tuple[str, str]:
        if self.alpaca_api_key is None or self.alpaca_secret_key is None:
            raise MissingCredentialsError(
                "Alpaca keys not set: put ALPACA_API_KEY and ALPACA_SECRET_KEY in .env "
                "(see .env.example) or the environment."
            )
        return self.alpaca_api_key.get_secret_value(), self.alpaca_secret_key.get_secret_value()

    def require_snaptrade_keys(self) -> tuple[str, str]:
        if self.snaptrade_client_id is None or self.snaptrade_consumer_key is None:
            raise MissingCredentialsError(
                "SnapTrade keys not set: put SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY in "
                ".env (personal API key from https://dashboard.snaptrade.com)."
            )
        return self.snaptrade_client_id, self.snaptrade_consumer_key.get_secret_value()


class MissingCredentialsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Universes:
    """Named symbol lists from ``config/universes.toml``."""

    bars: tuple[str, ...]
    options_underlyings: tuple[str, ...] = ()
    options_max_dte: int = 365
    watchlist: tuple[str, ...] = ()


def load_universes(path: Path) -> Universes:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    bars_section = raw.get("bars", {})
    bars = [*bars_section.get("etfs", []), *bars_section.get("stocks", [])]
    if len(set(bars)) != len(bars):
        dupes = sorted({s for s in bars if bars.count(s) > 1})
        raise ValueError(f"duplicate symbols in [bars] of {path}: {dupes}")
    options = raw.get("options", {})
    underlyings = list(options.get("underlyings", []))
    if len(set(underlyings)) != len(underlyings):
        raise ValueError(f"duplicate symbols in [options] of {path}")
    return Universes(
        bars=tuple(bars),
        options_underlyings=tuple(underlyings),
        options_max_dte=int(options.get("max_dte", 365)),
        watchlist=tuple(raw.get("dashboard", {}).get("watchlist", [])),
    )
