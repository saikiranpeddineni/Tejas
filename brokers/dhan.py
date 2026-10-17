"""
brokers/dhan.py — Dhan API v2 adapter.   ⚠️ UNTESTED w/o credentials.
=====================================================================
Env vars:
    DHAN_ACCESS_TOKEN     your Dhan access token
    DHAN_CLIENT_ID        your Dhan client id
    DHAN_INSTRUMENTS      JSON map {symbol: securityId} or path to it

Order placement needs the numeric NSE `securityId` per symbol (from Dhan's
instrument master CSV). Dhan access tokens are long-lived (validity set at
generation). Docs: https://dhanhq.co/docs/v2/
"""
import json
import logging
import os
from typing import List, Optional

from config import Config
from brokers.live_base import LiveBrokerBase

log = logging.getLogger(__name__)

BASE = "https://api.dhan.co/v2"
_PRODUCT = {"DELIVERY": "CNC", "INTRADAY": "INTRADAY"}


class DhanBroker(LiveBrokerBase):
    name = "dhan"

    def __init__(self):
        super().__init__()
        self.token  = os.getenv("DHAN_ACCESS_TOKEN", "")
        self.client = os.getenv("DHAN_CLIENT_ID", "")
        self.product = _PRODUCT.get(getattr(Config, "PRODUCT", "DELIVERY"), "CNC")
        self.secids = self._load_secids()
        self.session.headers.update({
            "access-token": self.token, "client-id": self.client,
            "Content-Type": "application/json", "Accept": "application/json",
        })
        if not (self.token and self.client):
            log.warning("[dhan] DHAN_ACCESS_TOKEN / DHAN_CLIENT_ID not set")
        log.info(f"[dhan] ready | product={self.product} | {len(self.secids)} securities mapped")

    def _load_secids(self) -> dict:
        raw = os.getenv("DHAN_INSTRUMENTS", "")
        if not raw:
            return {}
        try:
            if os.path.isfile(raw):
                with open(raw) as f:
                    return json.load(f)
            return json.loads(raw)
        except Exception as e:
            log.warning(f"[dhan] could not load security map: {e}")
            return {}

    def _secid(self, symbol: str) -> str:
        s = self.secids.get(symbol)
        if not s:
            raise ValueError(f"no securityId for {symbol} — populate DHAN_INSTRUMENTS")
        return str(s)

    def _account_info(self) -> dict:
        d = self._get(f"{BASE}/fundlimit")
        cash = float(d.get("availabelBalance", d.get("availableBalance", 0)) or 0)
        return {"cash": cash, "buying_power": cash, "portfolio_value": cash, "currency": "INR"}

    def _place_entry(self, symbol, side, qty, client_order_id: str = "") -> Optional[dict]:
        payload = {
            "dhanClientId": self.client, "transactionType": side.upper(),
            "exchangeSegment": "NSE_EQ", "productType": self.product,
            "orderType": "MARKET", "validity": "DAY",
            "securityId": self._secid(symbol), "quantity": int(qty), "price": 0,
        }
        if client_order_id:
            payload["correlationId"] = client_order_id
        body = self._post(f"{BASE}/orders", json=payload)
        order_id = body.get("orderId", "") if isinstance(body, dict) else ""
        return self._confirm_order(order_id, symbol, side, int(qty), client_order_id)

    def _order_status(self, order_id: str, client_order_id: str = "") -> dict:
        """Dhan's order-by-id endpoint returns the latest fill fields."""
        record = self._get(f"{BASE}/orders/{order_id}")
        if not isinstance(record, dict):
            record = {}
        return self._normalize_order_status(
            record,
            status=record.get("orderStatus"),
            order_id=order_id,
            client_order_id=client_order_id,
            filled_qty=record.get("filledQty"),
            filled_avg_price=record.get("averageTradedPrice"),
        )

    def _flatten(self, symbol) -> Optional[dict]:
        return self._flatten_position(symbol)

    def _positions(self) -> List[dict]:
        data = self._get(f"{BASE}/positions") or []
        out = []
        for p in data:
            if p.get("exchangeSegment") not in ("NSE_EQ", None):
                continue
            out.append({
                "symbol": p.get("tradingSymbol"),
                "qty": int(p.get("netQty", 0) or 0),
                "avg_entry_price": float(p.get("buyAvg", p.get("costPrice", 0)) or 0),
                "current_price": float(p.get("ltp", 0) or 0),
                "market_value": 0.0,
                "unrealized_pl": float(p.get("unrealizedProfit", 0) or 0),
            })
        return out
