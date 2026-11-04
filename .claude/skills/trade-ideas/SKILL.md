---
name: trade-ideas
description: Walk through today's trading ideas from the platform, with each one's gates checked. Use when the user asks what to trade, what's new today, or to go over the ideas or recommendations.
---

# Today's ideas, gate-checked

The platform makes the ideas; your job is to read them back clearly and check them. You never
place orders, and the ideas are rules applied to data, not advice. Say so if the user seems to
be treating them as certain.

1. `uv run tp-jobs status`: if the recommendations job or a data job has failed or is stale,
   say that first. Old data means old ideas. If the user has traded since the last brokerage
   sync (sold something, say), record it first with the `log-trade` skill so the sizes count
   the money it freed; `uv run tp-journal pending` shows what's already recorded.
2. `uv run tp-recs show` (add `--only swing` and similar if the user asked about one list). Start
   with the ideas starred ★ (new since the previous session), then anything listed under
   "On what you hold".
3. For each idea the user cares about (or the top two or three new ones), run
   `uv run tp-journal check SYMBOL` (add `--list NAME` if the ticker is on more than one list).
   Report the ✗ blocks plainly, with the reason the tool gives, then the ! warnings.
4. Give a short summary per idea: the setup, the structure, the entry limit and the date it's
   good until, the exit plan, the most it can lose, and what the gates say. Use the platform's
   numbers and don't invent any.

If the user decides to take or pass on one, use the `log-trade` skill.
