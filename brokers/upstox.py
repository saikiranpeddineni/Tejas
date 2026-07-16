"""
brokers/upstox.py — Upstox API v2 adapter.   ⚠️ UNTESTED w/o credentials.
=========================================================================
Env vars:
    UPSTOX_ACCESS_TOKEN   OAuth2 access token (regenerate daily via login flow)

Order placement needs an `instrument_key` (e.g. "NSE_EQ|INE002A01018"), not the
trading symbol. Provide a JSON map of {symbol: instrument_key} via the env var
UPSTOX_INSTRUMENTS (or a file path) — build it from Upstox's instruments dump:
https://upstox.com/developer/api-documentation/instruments
Docs: https://upstox.com/developer/api-documentation/
"""
import json
import logging
import os
from typing import List, Optional

from config import Config
from brokers.live_base import LiveBrokerBase

log = logging.getLogger(__name__)

BASE = "https://api.upstox.com/v2"
_PRODUCT = {"DELIVERY": "D", "INTRADAY": "I"}


class UpstoxBroker(LiveBrokerBase):
    name = "upstox"

    def __init__(self):
        super().__init__()
        self.token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
        self.product = _PRODUCT.get(getattr(Config, "PRODUCT", "DELIVERY"), "D")
        self.instruments = self._load_instruments()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        })
        if not self.token:
            log.warning("[upstox] UPSTOX_ACCESS_TOKEN not set")
        log.info(f"[upstox] ready | product={self.product} | {len(self.instruments)} instruments mapped")

    def _load_instruments(self) -> dict:
        raw = os.getenv("UPSTOX_INSTRUMENTS", "")
        if not raw:
            return {}
        try:
            if os.path.isfile(raw):
                with open(raw) as f:
                    return json.load(f)
            return json.loads(raw)
        except Exception as e:
            log.warning(f"[upstox] could not load instrument map: {e}")
            return {}

    def _ikey(self, symbol: str) -> str:
        key = self.instruments.get(symbol)
        if not key:
            raise ValueError(f"no instrument_key for {symbol} — populate UPSTOX_INSTRUMENTS")
        return key

    def _account_info(self) -> dict:
        d = self._get(f"{BASE}/user/get-funds-and-margin", params={"segment": "SEC"}).get("data", {})
        eq = d.get("equity", d)
        cash = float(eq.get("available_margin", 0) or 0)
        return {"cash": cash, "buying_power": cash, "portfolio_value": cash, "currency": "INR"}

    def _place_entry(self, symbol, side, qty) -> Optional[dict]:
        data = self._post(f"{BASE}/order/place", json={
            "quantity": int(qty), "product": self.product, "validity": "DAY",
            "price": 0, "instrument_token": self._ikey(symbol),
            "order_type": "MARKET", "transaction_type": side.upper(),
            "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False,
        }).get("data", {})
        return {"id": data.get("order_id", ""), "symbol": symbol, "qty": qty}

    def _flatten(self, symbol) -> Optional[dict]:
        pos = self.get_position(symbol)
        if not pos or int(pos.get("qty", 0)) == 0:
            return {"symbol": symbol, "qty": 0}
        return self._place_entry(symbol, "sell", abs(int(pos["qty"])))

    def _positions(self) -> List[dict]:
        data = self._get(f"{BASE}/portfolio/short-term-positions").get("data", [])
        out = []
        for p in data:
            out.append({
                "symbol": p.get("trading_symbol") or p.get("tradingsymbol"),
                "qty": int(p.get("quantity", 0)),
                "avg_entry_price": float(p.get("average_price", 0) or 0),
                "current_price": float(p.get("last_price", 0) or 0),
                "market_value": float(p.get("value", 0) or 0),
                "unrealized_pl": float(p.get("unrealised_pnl", p.get("pnl", 0)) or 0),
            })
        return out
