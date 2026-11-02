"""Discord alerts with a fake poster in place of the webhook."""

from __future__ import annotations

import json
import urllib.error

import pytest
from pydantic import SecretStr

from tp_core.alerts import LIMIT, Alerts, clip
from tp_core.config import Settings

URL = "https://discord.com/api/webhooks/1/x"


class Poster:
    def __init__(self, status: int = 204, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.sent: list[tuple[str, dict[str, object]]] = []

    def __call__(self, url: str, body: bytes) -> int:
        if self.error is not None:
            raise self.error
        self.sent.append((url, json.loads(body)))
        return self.status


def test_off_without_a_webhook() -> None:
    post = Poster()
    alerts = Alerts(None, post=post)
    assert not alerts.wants("failures")
    assert not alerts.send("failures", "x")
    assert post.sent == []


def test_kinds_come_from_the_settings(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(
        discord_webhook_url=SecretStr(URL), discord_alerts="failures, holdings,typo"
    )
    post = Poster()
    alerts = Alerts.from_settings(settings, post=post)
    assert "typo" in caplog.text
    assert alerts.kinds == {"failures", "holdings"}
    assert not alerts.send("ideas", "new ideas")
    assert alerts.send("failures", "bars gave up")
    url, body = post.sent[0]
    assert url == URL
    assert body == {"content": "bars gave up", "allowed_mentions": {"parse": []}}


def test_a_bad_webhook_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    assert not Alerts(URL, post=Poster(status=404)).send("failures", "x")
    down = Poster(error=urllib.error.URLError("offline"))
    assert not Alerts(URL, post=down).send("failures", "x")
    assert caplog.text.count("Discord alert not sent") == 2


def test_long_messages_are_clipped() -> None:
    post = Poster()
    Alerts(URL, post=post).send("ideas", "x" * (LIMIT + 50))
    content = post.sent[0][1]["content"]
    assert isinstance(content, str)
    assert len(content) == LIMIT
    assert content.endswith("…")
    assert clip("short") == "short"
