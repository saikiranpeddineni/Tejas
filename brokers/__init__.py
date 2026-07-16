"""
brokers — Pluggable broker layer for the Indian market (NSE equity cash).

Pick the active broker with the BROKER env var (see config.Config.BROKER):
    simulated | zerodha | upstox | angelone | dhan | fyers

All brokers expose the same Alpaca-compatible surface (see base.BaseBroker),
so the rest of Tejas is unchanged. Use the factory:

    from brokers import get_broker
    broker = get_broker()
"""
from brokers.factory import get_broker

__all__ = ["get_broker"]
