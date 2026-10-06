"""Strategy contract and event-driven engine.

One strategy class runs unchanged in the backtester and in paper trading:

    Strategy.on_bar(ctx)  ->  order intents (never broker orders)
        -> risk gate (outside strategy code)
            -> execution (simulated fills in a backtest, the paper broker otherwise)
                -> fills -> portfolio -> Strategy.on_fill(ctx, fill)
"""

__version__ = "0.1.0"
