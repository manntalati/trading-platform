---
name: log-trade
description: Record in the decision journal (tp-journal) that the user took, passed on, or exited a trade from the platform's ideas. Use when the user says they bought, sold, opened, closed, passed on or skipped an idea.
---

# Writing a decision in the journal

`tp-journal` records what the user did at their broker. It never orders anything, and neither do
you. Get the facts from the user and don't guess them.

**Took a trade.** You need the ticker, which list if it's on more than one, the contracts or
shares, and the fill price per share (for options, the price per share of the contract, like
`1.45`, not the $145 total).

1. `uv run tp-journal check SYMBOL --units N --price P` and show the user the result.
2. If nothing blocks: `uv run tp-journal take SYMBOL --units N --price P --note "..."`.
3. If a gate blocks, tell the user which gate and why. A trade that was placed must still be
   recorded: leaving it out would make the review look better than the trading was. So ask the
   user why they went ahead, and add `--override "reason"` in their own words. If the honest
   answer is that it was placed before the gates were checked, use
   `--override "placed before checking the gates"`, once the user says that's right. Never make a
   reason up, and never override the kill switch on your own initiative. The review keeps
   overridden trades separate, so the reason matters.
4. If it was placed before checking, suggest `uv run tp-journal check SYMBOL --units N` before
   ordering next time: the gates can only help ahead of the order.

**Passed on one.** `uv run tp-journal pass SYMBOL --note "why"`. Ask for a one-line why if they
didn't give one; a pass is worth as much to the review as a trade.

**Exited.** `uv run tp-journal list` to find the entry's number, then
`uv run tp-journal close ID PRICE --note "..."` with the exit price per share. Report the P&L
and the R the tool prints.

The journal file is under the data root, which is git-ignored. Never commit it, and never paste
it anywhere outside this machine.
