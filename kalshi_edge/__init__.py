"""Public Kalshi market data and a calibration backtest.

The collector reads unauthenticated market data into SQLite. The calibration
command asks whether contracts priced at X% before the event resolved YES
about X% of the time. Sportsbook comparison is not implemented.

No API keys are read or sent.
"""

__version__ = "0.2.0"
