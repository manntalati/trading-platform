"""Alerts to a Discord channel through a webhook (``TP_DISCORD_WEBHOOK_URL``).

Three kinds, each of which ``TP_DISCORD_ALERTS`` (comma-separated) can leave out:

    failures   a scheduled job gave up for its window (it failed until the window closed)
    ideas      ideas new since the previous session, once each per session
    holdings   a setup that fired on something you hold (a breakdown, a fade), once each

An alert is a nudge to look, not a decision: the dashboard and ``tp-recs show`` have the
numbers. Sending never raises: a webhook that's down or wrong is logged and the job carries on.
Only a line of text goes to Discord (names, setups, the job's last line of output), never
balances or positions' sizes. Whatever is sent is scrubbed first: the value of every secret in
the settings becomes ``***``, and so do a URL's query string and its user and password.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from pydantic import SecretStr

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


_URL_LOGIN = re.compile(r"(\w+://)[^\s/@]+@")
_URL_QUERY = re.compile(r"(\w+://[^\s?#]*)\?[^\s#]*")


SHORTEST_SECRET = 6  # a shorter "secret" would turn every match in a message into ***


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """``text`` with each of ``secrets`` and any URL's query string and login made ``***``."""
    secrets = {s for s in secrets if len(s) >= SHORTEST_SECRET}
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    text = _URL_LOGIN.sub(r"\1***@", text)
    return _URL_QUERY.sub(r"\1?***", text)


def secrets_of(settings: Settings) -> tuple[str, ...]:
    """The value of every secret (``SecretStr``) setting that is set."""
    values = (getattr(settings, name) for name in type(settings).model_fields)
    return tuple(v.get_secret_value() for v in values if isinstance(v, SecretStr))


@dataclass(frozen=True)
class Alerts:
    webhook: str | None
    kinds: frozenset[str] = KINDS
    post: Post = _post
    secrets: tuple[str, ...] = ()  # scrubbed out of whatever is sent

    @classmethod
    def from_settings(cls, settings: Settings, post: Post = _post) -> Alerts:
        url = settings.discord_webhook_url
        kinds = {k.strip() for k in settings.discord_alerts.split(",") if k.strip()}
        unknown = kinds - KINDS
        if unknown:
            log.warning("TP_DISCORD_ALERTS: ignoring unknown kind(s) %s", sorted(unknown))
        webhook = url.get_secret_value() if url else None
        return cls(webhook, frozenset(kinds & KINDS), post, secrets_of(settings))

    def wants(self, kind: str) -> bool:
        return self.webhook is not None and kind in self.kinds

    def send(self, kind: str, text: str) -> bool:
        """Post ``text`` if alerts of ``kind`` are on; whether Discord took it."""
        if not self.wants(kind) or self.webhook is None:
            return False
        content = clip(scrub(text, (*self.secrets, self.webhook)))
        body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
        try:
            status = self.post(self.webhook, body)
        except Exception as exc:  # whatever it was, an alert must not stop the job
            # only the type: an error's text can carry the webhook's URL
            log.warning("Discord alert not sent: %s", type(exc).__name__)
            return False
        if not 200 <= status < 300:
            log.warning("Discord alert not sent: HTTP %s", status)
            return False
        return True
