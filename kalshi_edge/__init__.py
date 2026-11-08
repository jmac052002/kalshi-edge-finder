"""kalshi-edge-finder: Kalshi market data, calibration, and sportsbook comparison.

``collect`` reads unauthenticated Kalshi market data into SQLite, ``calibrate``
backtests the pre-event prices against results, ``odds`` captures sportsbook
moneylines from The Odds API, and ``compare`` puts the two side by side.

Kalshi is read with no API key. The Odds API key is read from the
``ODDS_API_KEY`` environment variable in ``kalshi_edge.odds.main`` only.
"""

__version__ = "0.1.0"
