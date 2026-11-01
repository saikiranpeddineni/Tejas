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

    def _place_entry(self, symbol, side, qty, client_order_id: str = "") -> Optional[dict]:
        payload = {
            "symbol": self._fy(symbol), "qty": int(qty), "type": 2,  # 2 = market
            "side": 1 if side.lower() == "buy" else -1,
            "productType": self.product, "limitPrice": 0, "stopPrice": 0,
            "validity": "DAY", "disclosedQty": 0, "offlineOrder": False,
        }
        if client_order_id:
            payload["orderTag"] = client_order_id
        body = self._post(f"{BASE}/orders/sync", json=payload)
        order_id = body.get("id", "") if isinstance(body, dict) else ""
        # FYERS v3 identifies the order with the returned id; the client id is
        # retained through _confirm_order for local correlation and logging.
        return self._confirm_order(order_id, symbol, side, int(qty), client_order_id)

    def _order_status(self, order_id: str, client_order_id: str = "") -> dict:
        """FYERS orderbook-by-id returns the latest numeric order status."""
        body = self._get(f"{BASE}/orders", params={"id": order_id})
        records = body.get("orderBook", []) if isinstance(body, dict) else []
        if isinstance(records, dict):
            records = [records]
        records = records if isinstance(records, list) else []
        matches = [
            record for record in records
            if isinstance(record, dict)
            and str(record.get("id", record.get("orderId", ""))) == str(order_id)
        ]
        record = matches[-1] if matches else {}
        raw_status = record.get("status") if record else None
        if isinstance(raw_status, str) and raw_status.strip().isdigit():
            raw_status = int(raw_status.strip())
        if isinstance(raw_status, (int, float)) and not isinstance(raw_status, bool):
            raw_status = {
                1: "CANCELLED", 2: "TRADED", 4: "TRANSIT", 5: "REJECTED",
                6: "PENDING", 7: "PARTIALLY_FILLED", 8: "TRIGGER_PENDING",
                9: "CANCEL_PENDING", 10: "MODIFY_PENDING",
            }.get(int(raw_status), "UNKNOWN")
        return self._normalize_order_status(
            record,
            status=raw_status,
            order_id=order_id,
            client_order_id=client_order_id,
            filled_qty=record.get("filledQty") if record else None,
            filled_avg_price=(record.get("avgPrice") or record.get("averagePrice")) if record else None,
        )

    def _flatten(self, symbol) -> Optional[dict]:
        return self._flatten_position(symbol)

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
