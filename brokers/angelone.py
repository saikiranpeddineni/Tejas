"""
brokers/angelone.py — Angel One SmartAPI adapter.   ⚠️ UNTESTED w/o credentials.
================================================================================
Env vars:
    SMARTAPI_API_KEY      your SmartAPI app key
    SMARTAPI_JWT          session jwtToken (from loginByPassword + TOTP)
    SMARTAPI_INSTRUMENTS  JSON map {symbol: symboltoken} or path to it

SmartAPI sessions are created via /rest/auth/.../loginByPassword using
clientcode + pin + TOTP, which returns the jwtToken used here. Order placement
needs the numeric `symboltoken` per symbol (from the OpenAPI scrip master).
Docs: https://smartapi.angelbroking.com/docs
"""
import json
import logging
import os
from typing import List, Optional

from config import Config
from brokers.live_base import LiveBrokerBase

log = logging.getLogger(__name__)

BASE = "https://apiconnect.angelone.in"
_PRODUCT = {"DELIVERY": "DELIVERY", "INTRADAY": "INTRADAY"}


class AngelOneBroker(LiveBrokerBase):
    name = "angelone"

    def __init__(self):
        super().__init__()
        self.api_key = os.getenv("SMARTAPI_API_KEY", "")
        self.jwt     = os.getenv("SMARTAPI_JWT", "")
        self.product = _PRODUCT.get(getattr(Config, "PRODUCT", "DELIVERY"), "DELIVERY")
        self.tokens  = self._load_tokens()
        self.session.headers.update({
            "Authorization": f"Bearer {self.jwt}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-PrivateKey": self.api_key,
            "X-UserType": "USER", "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
        })
        if not (self.api_key and self.jwt):
            log.warning("[angelone] SMARTAPI_API_KEY / SMARTAPI_JWT not set")
        log.info(f"[angelone] ready | product={self.product} | {len(self.tokens)} tokens mapped")

    def _load_tokens(self) -> dict:
        raw = os.getenv("SMARTAPI_INSTRUMENTS", "")
        if not raw:
            return {}
        try:
            if os.path.isfile(raw):
                with open(raw) as f:
                    return json.load(f)
            return json.loads(raw)
        except Exception as e:
            log.warning(f"[angelone] could not load token map: {e}")
            return {}

    def _token(self, symbol: str) -> str:
        t = self.tokens.get(symbol)
        if not t:
            raise ValueError(f"no symboltoken for {symbol} — populate SMARTAPI_INSTRUMENTS")
        return str(t)

    def _account_info(self) -> dict:
        d = self._get(f"{BASE}/rest/secure/angelbroking/user/v1/getRMS").get("data")
        if not isinstance(d, dict):
            # On some auth/gateway errors Angel One returns data as a string or null.
            # _get already raises on status:false; this guards the residual case so
            # we never hit 'str object has no attribute get' masking the real error.
            raise RuntimeError(f"unexpected getRMS payload: {d!r}")
        cash = float(d.get("availablecash", d.get("net", 0)) or 0)
        return {"cash": cash, "buying_power": cash,
                "portfolio_value": float(d.get("net", cash) or cash), "currency": "INR"}

    def _place_entry(self, symbol, side, qty, client_order_id: str = "") -> Optional[dict]:
        payload = {
            "variety": "NORMAL", "tradingsymbol": f"{symbol}-EQ",
            "symboltoken": self._token(symbol), "transactiontype": side.upper(),
            "exchange": "NSE", "ordertype": "MARKET", "producttype": self.product,
            "duration": "DAY", "quantity": str(int(qty)), "price": "0",
        }
        if client_order_id:
            payload["ordertag"] = client_order_id
        body = self._post(f"{BASE}/rest/secure/angelbroking/order/v1/placeOrder", json=payload)
        data = body.get("data", {}) if isinstance(body, dict) else {}
        order_id = data.get("orderid", "") if isinstance(data, dict) else ""
        return self._confirm_order(order_id, symbol, side, int(qty), client_order_id)

    def _order_status(self, order_id: str, client_order_id: str = "") -> dict:
        """SmartAPI order-detail endpoint returns one order's current state."""
        record = self._get(
            f"{BASE}/rest/secure/angelbroking/order/v1/details/{order_id}"
        )
        record = record.get("data", {}) if isinstance(record, dict) else {}
        if not isinstance(record, dict):
            record = {}
        return self._normalize_order_status(
            record,
            status=(record.get("orderstatus") or record.get("orderStatus")) if record else None,
            order_id=order_id,
            client_order_id=client_order_id,
            filled_qty=(record.get("filledshares") or record.get("filledquantity")) if record else None,
            filled_avg_price=(record.get("averageprice") or record.get("avgprice")) if record else None,
        )

    def _flatten(self, symbol) -> Optional[dict]:
        return self._flatten_position(symbol)

    def _positions(self) -> List[dict]:
        data = self._get(f"{BASE}/rest/secure/angelbroking/order/v1/getPosition").get("data") or []
        out = []
        for p in data:
            if p.get("exchange") != "NSE":
                continue
            sym = (p.get("tradingsymbol") or "").replace("-EQ", "")
            out.append({
                "symbol": sym,
                "qty": int(float(p.get("netqty", 0) or 0)),
                "avg_entry_price": float(p.get("avgnetprice", p.get("netprice", 0)) or 0),
                "current_price": float(p.get("ltp", 0) or 0),
                "market_value": 0.0,
                "unrealized_pl": float(p.get("unrealised", p.get("pnl", 0)) or 0),
            })
        return out
