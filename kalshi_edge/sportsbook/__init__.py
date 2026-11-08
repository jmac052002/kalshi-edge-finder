"""Sportsbook odds: fetching them, storing them, and matching them to Kalshi.

The one secret in this project, the Odds API key, enters through
``kalshi_edge.odds.main`` and is handed to ``OddsApiClient``. Nothing in
this package reads the environment, and every log line and exception the
client produces has the key scrubbed out (see ``client.redact``).
"""
