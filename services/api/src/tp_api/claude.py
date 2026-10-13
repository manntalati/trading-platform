"""Asking Claude about your positions and research ideas, through the Anthropic API.

Optional: needs ``ANTHROPIC_API_KEY``. Each question sends the figures it's about (and a summary
of the portfolio) to Anthropic's API and is billed to that key's account.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Protocol

import anthropic
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from tp_core.config import MissingCredentialsError, Settings

log = logging.getLogger(__name__)

MAX_TOKENS = 16_000
MAX_CONTINUATIONS = 3  # web searches can pause a turn; resume it at most this often
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4_000)
    symbol: str | None = Field(default=None, max_length=40)
    history: list[Turn] = Field(default_factory=list, max_length=20)


@dataclass(frozen=True)
class Answer:
    text: str
    model: str
    stop_reason: str | None
    sources: list[dict[str, str]] = field(default_factory=list)
    searches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    note: str | None = None


class Advisor(Protocol):
    def ask(self, system: str, messages: list[dict[str, Any]]) -> Answer: ...


class ClaudeAdvisor:
    """Claude through the Anthropic API: adaptive thinking at the configured effort, streamed
    (long answers can take a while), with server-side fallback to another model if a request
    is declined, and optional web search."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            key = self.settings.require_anthropic_key()
            self._client = anthropic.Anthropic(api_key=key, timeout=300.0)
        return self._client

    def ask(self, system: str, messages: list[dict[str, Any]]) -> Answer:
        tools = (
            [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
            if self.settings.ask_web_search
            else []
        )
        try:
            return self._ask(system, messages, tools)
        except anthropic.BadRequestError as exc:
            if not tools or "web_search" not in str(exc):
                raise
            log.warning("web search unavailable, asking without it: %s", exc)
            answer = self._ask(system, messages, [])
            return replace(answer, note="Web search isn't enabled for this API key.")

    def _ask(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Answer:
        convo = list(messages)
        blocks: list[Any] = []
        usage = [0, 0]
        message: Any = None
        for _ in range(MAX_CONTINUATIONS + 1):
            extra: dict[str, Any] = {"tools": tools} if tools else {}
            with self.client().beta.messages.stream(
                model=self.settings.ask_model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=convo,
                output_config={"effort": self.settings.ask_effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
                **extra,
            ) as stream:
                message = stream.get_final_message()
            blocks += message.content
            usage[0] += message.usage.input_tokens
            usage[1] += message.usage.output_tokens
            if message.stop_reason != "pause_turn":
                break
            convo = [*convo, {"role": "assistant", "content": message.content}]
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            why = getattr(details, "explanation", None) if details else None
            text = "Claude declined to answer this one." + (f" ({why})" if why else "")
            return Answer(text, message.model, "refusal", input_tokens=usage[0],
                          output_tokens=usage[1])  # fmt: skip
        return Answer(
            text="".join(b.text for b in blocks if b.type == "text").strip(),
            model=message.model,
            stop_reason=message.stop_reason,
            sources=sources(blocks),
            searches=sum(
                1 for b in blocks if b.type == "server_tool_use" and b.name == "web_search"
            ),
            input_tokens=usage[0],
            output_tokens=usage[1],
        )


def sources(blocks: list[Any]) -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    for b in blocks:
        for c in getattr(b, "citations", None) or []:
            url = getattr(c, "url", None)
            if url and url not in seen:
                seen[url] = getattr(c, "title", None) or url
    return [{"url": u, "title": t} for u, t in seen.items()]


def rounded(value: Any) -> Any:
    """Four significant figures is plenty, and keeps the context (and the bill) small."""
    if isinstance(value, dict):
        return {k: rounded(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [rounded(v) for v in value]
    if isinstance(value, float):
        return float(f"{value:.4g}") if math.isfinite(value) else None
    return value


def ask_claude(request: Request, system: str, body: AskRequest, now: datetime) -> dict[str, Any]:
    """Send ``body``'s conversation with ``system`` to the app's advisor; HTTP errors for the
    usual failures (no key, API down)."""
    roles = [t.role for t in body.history]
    if roles != ["user", "assistant"] * (len(roles) // 2):
        raise HTTPException(422, "history must alternate user, assistant, starting with user")
    messages = [{"role": t.role, "content": t.content} for t in body.history]
    messages.append({"role": "user", "content": body.question})
    advisor: Advisor = request.app.state.advisor
    try:
        answer = advisor.ask(system, messages)
    except MissingCredentialsError as exc:
        raise HTTPException(503, str(exc)) from exc
    except anthropic.APIStatusError as exc:
        raise HTTPException(502, f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(502, f"couldn't reach the Claude API: {exc}") from exc
    return {
        "answer": answer.text,
        "model": answer.model,
        "stop_reason": answer.stop_reason,
        "sources": answer.sources,
        "searches": answer.searches,
        "usage": {"input_tokens": answer.input_tokens, "output_tokens": answer.output_tokens},
        "note": answer.note,
        "as_of": now.isoformat(),
    }
