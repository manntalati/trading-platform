"""Alerts to a Discord channel through a webhook (``TP_DISCORD_WEBHOOK_URL``).

Three kinds, each of which ``TP_DISCORD_ALERTS`` (comma-separated) can leave out:

    failures   a scheduled job gave up for its window (it failed until the window closed)
    ideas      ideas new since the previous session, once each per session
    holdings   a setup that fired on something you hold (a breakdown, a fade), once each

An alert is a nudge to look, not a decision: the dashboard and ``tp-recs show`` have the
numbers. Sending never raises: a webhook that's down or wrong is logged and the job carries on.
Only a line of text goes to Discord (names, setups, the job's last line of output), never
balances or positions' sizes.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from tp_core.config import Settings

log = logging.getLogger(__name__)

KINDS = frozenset({"failures", "ideas", "holdings"})
LIMIT = 2000  # Discord's limit on a message's content
TIMEOUT = 10.0
# Discord's edge rejects urllib's default User-Agent; it asks for "DiscordBot (url, version)".
USER_AGENT = "DiscordBot (https://github.com/manntalati/trading-platform, 0.1)"

Post = Callable[[str, bytes], int]  # (url, JSON body) -> HTTP status


def _post(url: str, body: bytes) -> int:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return int(response.status)


def clip(text: str, limit: int = LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class Alerts:
    webhook: str | None
    kinds: frozenset[str] = KINDS
    post: Post = _post

    @classmethod
    def from_settings(cls, settings: Settings, post: Post = _post) -> Alerts:
        url = settings.discord_webhook_url
        kinds = {k.strip() for k in settings.discord_alerts.split(",") if k.strip()}
        unknown = kinds - KINDS
        if unknown:
            log.warning("TP_DISCORD_ALERTS: ignoring unknown kind(s) %s", sorted(unknown))
        return cls(url.get_secret_value() if url else None, frozenset(kinds & KINDS), post)

    def wants(self, kind: str) -> bool:
        return self.webhook is not None and kind in self.kinds

    def send(self, kind: str, text: str) -> bool:
        """Post ``text`` if alerts of ``kind`` are on; whether Discord took it."""
        if not self.wants(kind) or self.webhook is None:
            return False
        body = json.dumps({"content": clip(text), "allowed_mentions": {"parse": []}}).encode()
        try:
            status = self.post(self.webhook, body)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("Discord alert not sent: %s", exc)
            return False
        if not 200 <= status < 300:
            log.warning("Discord alert not sent: HTTP %s", status)
            return False
        return True
