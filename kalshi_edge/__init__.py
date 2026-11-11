"""kalshi-edge-finder: Kalshi market data, calibration, sportsbook comparison, and a non-sports scan.

``collect`` reads unauthenticated Kalshi market data into SQLite, ``calibrate``
backtests the pre-event prices against results, ``odds`` captures sportsbook
moneylines from The Odds API, ``compare`` puts the two side by side, and
``scan`` ranks short-dated non-sports markets against an NWS forecast or a
Cleveland Fed nowcast.

Kalshi is read with no API key. The Odds API key is read from the
``ODDS_API_KEY`` environment variable in ``kalshi_edge.odds.main`` only.
``scan`` never places an order.
"""

__version__ = "0.1.0"
