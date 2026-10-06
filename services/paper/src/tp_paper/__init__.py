"""Paper trading: library strategies trade Alpaca's paper account, one capital sleeve each.

    after the close   propose   sync fills, mark sleeves, run strategies, risk-check, queue
                                (approval = "auto" approves on the spot; "manual" waits for you)
    before the open   submit    queued proposals become market-on-open orders
    after the open    sync      record fills, reconcile with the broker's positions

``tp-paper bot`` runs those steps on the exchange calendar by itself (``tp_paper.bot``); the
individual ``tp-paper`` commands run one step by hand. There is no live-trading mode in this
package: the broker client is always created with ``paper=True``.
"""

__version__ = "0.1.0"
