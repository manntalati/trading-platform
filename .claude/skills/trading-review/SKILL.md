---
name: trading-review
description: Review how the user's own decisions and the platform's ideas have done, from the decision journal and the recommendations scorecard. Use for a weekly or monthly review, or when the user asks how their trading is going.
---

# Reviewing the decisions

1. `uv run tp-journal review`: how many trades taken, passed on and still open; the win rate, P&L
   and mean R overall; the same for trades that followed the gates and trades that overrode one;
   and the results by setup.
2. `uv run tp-journal list --limit 50` for the individual entries and their notes.
3. `uv run tp-recs scorecard`: how every idea did, whether or not it was taken.

Then compare:

- **Followed the gates against overrode a gate.** If overrides do worse, say so plainly.
- **What was taken against the scorecard.** Is the user picking the better ideas or the worse
  ones? Are they passing on setups that went on to work?
- **Open trades past their exit plan's date**, from `tp-journal list` and the plan in
  `tp-recs show`.

Be honest about sample size. A handful of closed trades says very little, so put "too few to
tell" ahead of any pattern. Don't suggest changing the gates' settings (`TP_JOURNAL_MAX_PER_DAY`,
`TP_JOURNAL_LOSS_STREAK`, `TP_OPTIONS_RISK`, `config/risk.toml`) on less than a few dozen trades,
and leave the decision to the user.
