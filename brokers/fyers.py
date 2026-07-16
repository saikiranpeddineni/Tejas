"""
brokers/fyers.py — Fyers API v3 adapter.   ⚠️ UNTESTED w/o credentials.
=======================================================================
Env vars:
    FYERS_APP_ID          your Fyers app id (client id)
    FYERS_ACCESS_TOKEN    daily access token (from the auth-code login flow)

Fyers uses symbol strings like "NSE:RELIANCE-EQ" — no instrument-token lookup
needed. Access tokens are regenerated daily via the auth flow.
Docs: https://myapi.fyers.in/docsv3
"""
import logging
import os
from typing import List, Optional

from config import Config
from brokers.live_base import LiveBrokerBase

log = logging.getLogger(__name__)

BASE = "https://api-t1.fyers.in/api/v3"
_PRODUCT = {"DELIVERY": "CNC", "INTRADAY": "INTRADAY"}


class FyersBroker(LiveBrokerBase):
    name = "fyers"

    def __init__(self):
        super().__init__()
        self.app_id = os.getenv("FYERS_APP_ID", "")
        self.token  = os.getenv("FYERS_ACCESS_TOKEN", "")
        self.product = _PRODUCT.get(getattr(Config, "PRODUCT", "DELIVERY"), "CNC")
        self.session.headers.update({
            "Authorization": f"{self.app_id}:{self.token}",
            "Content-Type": "application/json",
        })
        if not (self.app_id and self.token):
            log.warning("[fyers] FYERS_APP_ID / FYERS_ACCESS_TOKEN not set")
        log.info(f"[fyers] ready | product={self.product}")

    @staticmethod
    def _fy(symbol: str) -> str:
        return f"NSE:{symbol}-EQ"

    def _account_info(self) -> dict:
        funds = self._get(f"{BASE}/funds").get("fund_limit", [])
        cash = 0.0
        for f in funds:
            # title 10 == "Available Balance" in Fyers' funds payload
            if str(f.get("id")) == "10" or "available" in str(f.get("title", "")).lower():
                cash = float(f.get("equityAmount", f.get("amount", 0)) or 0)
                break
        return {"cash": cash, "buying_power": cash, "portfolio_value": cash, "currency": "INR"}

    def _place_entry(self, symbol, side, qty) -> Optional[dict]:
        data = self._post(f"{BASE}/orders/sync", json={
            "symbol": self._fy(symbol), "qty": int(qty), "type": 2,  # 2 = market
            "side": 1 if side.lower() == "buy" else -1,
            "productType": self.product, "limitPrice": 0, "stopPrice": 0,
            "validity": "DAY", "disclosedQty": 0, "offlineOrder": False,
        })
        return {"id": data.get("id", ""), "symbol": symbol, "qty": qty}

    def _flatten(self, symbol) -> Optional[dict]:
        pos = self.get_position(symbol)
        if not pos or int(pos.get("qty", 0)) == 0:
            return {"symbol": symbol, "qty": 0}
        return self._place_entry(symbol, "sell", abs(int(pos["qty"])))

    def _positions(self) -> List[dict]:
        data = self._get(f"{BASE}/positions").get("netPositions", []) or []
        out = []
        for p in data:
            sym = (p.get("symbol", "")).replace("NSE:", "").replace("-EQ", "")
            qty = int(p.get("netQty", 0) or 0)
            if qty == 0:
                continue
            out.append({
                "symbol": sym, "qty": qty,
                "avg_entry_price": float(p.get("buyAvg", p.get("netAvg", 0)) or 0),
                "current_price": float(p.get("ltp", 0) or 0),
                "market_value": float(p.get("netValue", 0) or 0),
                "unrealized_pl": float(p.get("pl", 0) or 0),
            })
        return out
