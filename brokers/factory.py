"""
brokers/factory.py — Select the active broker from Config.BROKER.
"""
import logging

from config import Config

log = logging.getLogger(__name__)


def get_broker():
    """Return a broker instance for Config.BROKER (default: simulated)."""
    name = (getattr(Config, "BROKER", "simulated") or "simulated").lower()

    if name == "simulated":
        from brokers.simulated import SimulatedBroker
        return SimulatedBroker()
    if name == "zerodha":
        from brokers.zerodha import ZerodhaBroker
        return ZerodhaBroker()
    if name == "upstox":
        from brokers.upstox import UpstoxBroker
        return UpstoxBroker()
    if name in ("angelone", "angel", "smartapi"):
        from brokers.angelone import AngelOneBroker
        return AngelOneBroker()
    if name == "dhan":
        from brokers.dhan import DhanBroker
        return DhanBroker()
    if name == "fyers":
        from brokers.fyers import FyersBroker
        return FyersBroker()

    log.error(f"[factory] Unknown BROKER='{name}' — falling back to simulated")
    from brokers.simulated import SimulatedBroker
    return SimulatedBroker()
