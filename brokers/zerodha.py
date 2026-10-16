"""
brokers/zerodha.py — Zerodha Kite Connect adapter.   ⚠️ UNTESTED w/o credentials.
================================================================================
Env vars:
    KITE_API_KEY          your Kite Connect app key
    KITE_ACCESS_TOKEN     daily access token (from the login/request-token flow)

Access tokens expire daily; regenerate via the Kite login flow (kiteconnect
generate_session) and update KITE_ACCESS_TOKEN before market open.
Docs: https://kite.trade/docs/connect/v3/
"""
import logging
import os
from typing import List, Optional

from config import Config
from brokers.live_base import LiveBrokerBase

log = logging.getLogger(__name__)

BASE = "https://api.kite.trade"
_PRODUCT = {"DELIVERY": "CNC", "INTRADAY": "MIS"}


class ZerodhaBroker(LiveBrokerBase):
    name = "zerodha"

    def __init__(self):
        super().__init__()
        self.api_key = os.getenv("KITE_API_KEY", "")
        self.token   = os.getenv("KITE_ACCESS_TOKEN", "")
        self.product = _PRODUCT.get(getattr(Config, "PRODUCT", "DELIVERY"), "CNC")
        self.session.headers.update({
            "X-Kite-Version": "3",
            "Authorization": f"token {self.api_key}:{self.token}",
        })
        if not (self.api_key and self.token):
            log.warning("[zerodha] KITE_API_KEY / KITE_ACCESS_TOKEN not set")
        log.info(f"[zerodha] ready | product={self.product}")

    def _account_info(self) -> dict:
        d = self._get(f"{BASE}/user/margins").get("data", {})
        eq = d.get("equity", {})
        cash = float(eq.get("available", {}).get("live_balance", eq.get("net", 0)) or 0)
        return {"cash": cash, "buying_power": cash,
                "portfolio_value": float(eq.get("net", cash) or cash), "currency": "INR"}

    def _place_entry(self, symbol, side, qty, client_order_id: str = "") -> Optional[dict]:
        payload = {
            "exchange": "NSE", "tradingsymbol": symbol,
            "transaction_type": side.upper(), "quantity": int(qty),
            "order_type": "MARKET", "product": self.product, "validity": "DAY",
        }
        if client_order_id:
            payload["tag"] = client_order_id
        data = self._post(f"{BASE}/orders/regular", data=payload).get("data", {})
        return {"id": data.get("order_id", ""), "symbol": symbol, "qty": qty}

    def _flatten(self, symbol) -> Optional[dict]:
        pos = self._get_position_strict(symbol)
        qty = int(float(pos.get("qty", 0) or 0)) if pos else 0
        if qty == 0:
            return {"symbol": symbol, "qty": 0}
        return self._place_entry(symbol, "sell" if qty > 0 else "buy", abs(qty))

    def _positions(self) -> List[dict]:
        net = self._get(f"{BASE}/portfolio/positions").get("data", {}).get("net", [])
        out = []
        for p in net:
            if p.get("exchange") != "NSE":
                continue
            out.append({
                "symbol": p.get("tradingsymbol"),
                "qty": int(p.get("quantity", 0)),
                "avg_entry_price": float(p.get("average_price", 0) or 0),
                "current_price": float(p.get("last_price", 0) or 0),
                "market_value": float(p.get("value", 0) or 0),
                "unrealized_pl": float(p.get("unrealised", p.get("pnl", 0)) or 0),
            })
        return out
