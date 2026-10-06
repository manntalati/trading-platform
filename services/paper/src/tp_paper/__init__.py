"""Paper trading: library strategies trade Alpaca's paper account, one capital sleeve each.

    after the close   tp-paper propose   sync fills, mark sleeves, run strategies, risk-check
    evening/morning   you approve        dashboard or `tp-paper approve` (or approval = "auto")
    before the open   tp-paper submit    approved proposals become market-on-open orders
    after the open    tp-paper sync      record fills, reconcile with the broker's positions

There is no live-trading mode in this package: the broker client is always created with
``paper=True``.
"""

__version__ = "0.1.0"
