"""Scoring headlines with Claude: one number per headline, -1 to +1, for how good the news is for
the stocks it names (``tp_ingest.jobs.news.run_scoring``).

Each request scores a batch of headlines and must answer in a fixed JSON shape (structured
output), so a reply always parses; scores outside -1..1 are clipped and ids it wasn't asked
about are dropped. The model reads only the headline and the start of its summary, as a reader
would have at publication.
"""

from __future__ import annotations

import json
from typing import Any, cast

PROMPT = """\
You score financial news headlines for a stock-ranking model. For each headline give one number \
from -1 to 1: how good the news is for the stock of the companies it names, as an informed \
investor reading it when it was published would judge it.

-1: clearly bad (a guidance cut, a large miss, fraud, a failed trial, a downgrade with a big \
target cut). 0: neutral, mixed, routine coverage, or not really about the company. +1: clearly \
good (a large beat, raised guidance, a major contract, an approval, an upgrade).

Judge only the text given. Don't use anything you know about what happened afterwards. Score \
every id once."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "score": {"type": "number"}},
                "required": ["id", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}


class ClaudeScorer:
    def __init__(self, api_key: str, model: str, effort: str = "low") -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key, timeout=180.0)
        self.model = model
        self.effort = effort

    def score(self, items: list[dict[str, str]]) -> dict[str, float]:
        lines = "\n".join(json.dumps(i, ensure_ascii=False) for i in items)
        from anthropic.types import MessageParam, OutputConfigParam

        messages: list[MessageParam] = [
            {"role": "user", "content": f"{PROMPT}\n\n<headlines>\n{lines}\n</headlines>"}
        ]
        config = cast(
            OutputConfigParam,
            {"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        response = self._client.messages.create(
            model=self.model, max_tokens=16000, messages=messages, output_config=config
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"no scores: the reply stopped ({response.stop_reason})")
        text = next(b.text for b in response.content if b.type == "text")
        return parse_scores(text, {i["id"] for i in items})


def parse_scores(text: str, wanted: set[str]) -> dict[str, float]:
    """The reply's scores for the ids asked about, clipped to -1..1."""
    data = json.loads(text)
    out: dict[str, float] = {}
    for s in data.get("scores", []):
        key = str(s.get("id"))
        try:
            value = float(s.get("score"))
        except (TypeError, ValueError):
            continue
        if key in wanted and value == value:
            out[key] = max(-1.0, min(1.0, value))
    return out
