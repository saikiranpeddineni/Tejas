"""
brokers/live_base.py — Shared base for real Indian broker adapters.
===================================================================
Market DATA (bars/quotes, hence regime + features) comes from Yahoo by default,
so every live adapter only has to implement the trading-critical hooks:

    _account_info()         -> dict (cash, buying_power, portfolio_value)
    _place_entry(sym,side,qty,client_order_id) -> dict with a confirmed fill
    _order_status(order_id, client_order_id)   -> normalized order status
    _flatten(symbol)        -> dict
    _positions()            -> List[dict]

A broker's order-placement response only acknowledges submission.  The shared
helpers below poll the adapter's read-only order-status endpoint and expose a
fill to the rest of Tejas only after a terminal filled/complete state contains
an explicit positive filled quantity and average price.

⚠️  These adapters are written against each broker's published REST API but are
    UNTESTED without live credentials. Validate carefully (start with tiny
    quantities) before trusting real money. Auth tokens for most Indian brokers
    are short-lived (often regenerated daily via a login/TOTP flow) — see each
    adapter's docstring.
"""
import logging
import math
import time
from typing import Dict, List, Optional

from models import Bar, Quote
from brokers.base import BaseBroker
from brokers.data_provider import YahooDataProvider, make_session

log = logging.getLogger(__name__)


# These are deliberately small, bounded defaults.  They are class attributes on
# LiveBrokerBase as well, so a deployment or an offline fake can tune them
# without changing the public broker API or adding a dependency/config surface.
ORDER_STATUS_POLL_INTERVAL = 0.5
ORDER_STATUS_POLL_TIMEOUT = 8.0
ORDER_STATUS_MAX_POLLS = 16

ORDER_STATUS_FILLED = "filled"
ORDER_STATUS_PENDING = "pending"
ORDER_STATUS_REJECTED = "rejected"
ORDER_STATUS_CANCELLED = "cancelled"
ORDER_STATUS_EXPIRED = "expired"
ORDER_STATUS_TIMEOUT = "timeout"
ORDER_STATUS_UNKNOWN = "unknown"
_ORDER_TERMINAL = frozenset({
    ORDER_STATUS_FILLED,
    ORDER_STATUS_REJECTED,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_EXPIRED,
    ORDER_STATUS_TIMEOUT,
})

_STATUS_ALIASES = {
    ORDER_STATUS_FILLED: frozenset({
        "complete", "completed", "filled", "fill", "traded", "executed",
        "execution complete",
    }),
    ORDER_STATUS_REJECTED: frozenset({"reject", "rejected", "failed", "failure"}),
    ORDER_STATUS_CANCELLED: frozenset({"cancel", "cancelled", "canceled"}),
    ORDER_STATUS_EXPIRED: frozenset({"expire", "expired"}),
    ORDER_STATUS_TIMEOUT: frozenset({"timeout", "timed out", "time out"}),
    ORDER_STATUS_PENDING: frozenset({
        "open", "pending", "transit", "part traded", "partial", "partially filled",
        "trigger pending", "put order req received", "validation pending",
        "open pending", "modify pending", "modified", "cancel pending",
        "pending cancel", "pending replace", "amo req received", "received",
    }),
}


def _extract_error(body) -> Optional[str]:
    """Pull a human-readable broker error out of a 200-with-error or non-2xx body.
    Handles the differing conventions: AngelOne {status:false,message,errorcode},
    Zerodha/Upstox {status:"error",message,error_type} or {errors:[...]}.
    """
    if not isinstance(body, dict):
        return None
    st = body.get("status", body.get("s"))
    is_err = (st is False) or (isinstance(st, str) and st.lower() in ("error", "false"))
    if body.get("success") is False:
        is_err = True
    # Upstox: {"status":"error","errors":[{"message":...,"errorCode":...}]}
    errs = body.get("errors")
    if not is_err and not errs:
        return None
    if isinstance(errs, list) and errs:
        e0 = errs[0] if isinstance(errs[0], dict) else {}
        return str(e0.get("message") or e0.get("errorCode") or errs)
    return str(body.get("message") or body.get("errmsg") or body.get("error_type")
               or body.get("errorcode") or body.get("code") or body.get("error") or body)


def _first(payload: dict, *keys):
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _finite_number(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _whole_quantity(value) -> Optional[int]:
    """Return a positive whole-share quantity, or None for malformed input."""
    number = _finite_number(value)
    if number is None or number <= 0 or abs(number - round(number)) > 1e-9:
        return None
    return int(round(number))


def _canonical_status(value) -> str:
    """Map broker-specific text to the small status vocabulary used by callers."""
    if isinstance(value, bool) or value is None:
        return ORDER_STATUS_UNKNOWN
    if isinstance(value, (int, float)):
        # Numeric status codes are intentionally adapter-owned.  Treating an
        # arbitrary broker code as filled would be unsafe.
        return ORDER_STATUS_UNKNOWN
    text = " ".join(str(value).strip().lower().replace("_", " ").replace("-", " ").split())
    if not text:
        return ORDER_STATUS_UNKNOWN
    for canonical, aliases in _STATUS_ALIASES.items():
        if text in aliases:
            return canonical
    if "reject" in text or "fail" in text:
        return ORDER_STATUS_REJECTED
    if "cancel" in text:
        return ORDER_STATUS_CANCELLED
    if "expir" in text:
        return ORDER_STATUS_EXPIRED
    if "partial" in text or "pending" in text or "open" in text or "transit" in text:
        return ORDER_STATUS_PENDING
    return ORDER_STATUS_UNKNOWN


class LiveBrokerBase(BaseBroker):
    name = "live"

    # Instance-tunable bounded polling defaults.  No POST is retried by this
    # path; only the read-only status GET is repeated.
    ORDER_STATUS_POLL_INTERVAL = ORDER_STATUS_POLL_INTERVAL
    ORDER_STATUS_POLL_TIMEOUT = ORDER_STATUS_POLL_TIMEOUT
    ORDER_STATUS_MAX_POLLS = ORDER_STATUS_MAX_POLLS

    def __init__(self):
        super().__init__()
        self.data = YahooDataProvider()
        self.session = make_session()   # pooled + GET-retrying session

    # Data hooks delegate to Yahoo (NSE). Override if you wire native data.
    def _fetch_bars(self, symbol, timeframe="1Day", limit=100, start=None) -> List[Bar]:
        return self.data.get_bars(symbol, timeframe, limit, start)

    def _fetch_quote(self, symbol) -> Optional[Quote]:
        return self.data.get_quote(symbol)

    def _fetch_bars_bulk(self, symbols, timeframe="1Day", limit=100) -> Dict[str, List[Bar]]:
        return self.data.get_bars_bulk(symbols, timeframe, limit)

    def _fetch_quotes_bulk(self, symbols) -> Dict[str, Quote]:
        return self.data.get_quotes_bulk(symbols)

    # Helpers
    def _check(self, r):
        """Parse a broker response and raise a RuntimeError carrying the broker's
        REAL error message. Two failure modes are both surfaced (instead of the
        old masking): (1) non-2xx HTTP — include the body, not just "401"; and
        (2) HTTP 200 with an error envelope (status:false / status:"error")."""
        try:
            body = r.json()
        except Exception:
            body = None
        if not r.ok:
            detail = _extract_error(body) or (r.text or "")[:300]
            raise RuntimeError(f"HTTP {r.status_code}: {detail}")
        err = _extract_error(body)
        if err:
            raise RuntimeError(f"broker error: {err}")
        return body

    def _get(self, url, **kw):
        return self._check(self.session.get(url, timeout=10, **kw))

    def _post(self, url, **kw):
        # make_session only retries GET.  Keep this wrapper one-shot: retrying
        # an order POST after a lost response can create a duplicate live order.
        return self._check(self.session.post(url, timeout=10, **kw))

    @staticmethod
    def _normalize_order_status(
        payload,
        *,
        status=None,
        order_id="",
        client_order_id=None,
        filled_qty=None,
        filled_avg_price=None,
    ) -> dict:
        """Return the normalized order-status shape used by the poller.

        Adapters pass the fields documented by their status endpoint.  The
        explicit field lists avoid accidentally treating submitted quantity or
        limit price as a fill.
        """
        payload = payload if isinstance(payload, dict) else {}
        raw_status = status if status is not None else _first(
            payload, "status", "order_status", "orderStatus", "orderstate", "orderState"
        )
        payload_order_id = _first(payload, "id", "order_id", "orderId", "orderid")
        broker_order_id = str(payload_order_id or order_id or "").strip()
        if filled_qty is None:
            filled_qty = _first(
                payload, "filled_quantity", "filledQuantity", "filled_qty", "filledQty",
                "filledshares", "filledShares",
            )
        if filled_avg_price is None:
            filled_avg_price = _first(
                payload, "filled_avg_price", "filledAvgPrice", "average_price", "averagePrice",
                "avg_price", "avgPrice", "avgTradedPrice", "tradedPrice",
            )
        payload_client_order_id = _first(
            payload, "client_order_id", "clientOrderId", "tag", "orderTag",
            "ordertag", "correlationId", "correlation_id",
        )
        # The expected id is only a fallback.  If the broker sends an id/tag,
        # retain it so _confirm_order can reject a correlation mismatch.
        normalized_client_order_id = (
            payload_client_order_id
            if payload_client_order_id not in (None, "")
            else client_order_id
        )
        qty = _finite_number(filled_qty) if filled_qty is not None else None
        price = _finite_number(filled_avg_price) if filled_avg_price is not None else None
        return {
            "status": _canonical_status(raw_status),
            "broker_status": raw_status,
            "id": broker_order_id,
            "client_order_id": str(normalized_client_order_id).strip() if normalized_client_order_id else "",
            "filled_qty": qty,
            "filled_avg_price": price,
        }

    def _order_status(self, order_id: str, client_order_id: str = "") -> dict:
        """Fetch one status snapshot; each live adapter implements this GET."""
        raise NotImplementedError

    def _fill_is_valid(self, status: dict, requested_qty: Optional[int] = None) -> bool:
        qty = _finite_number(status.get("filled_qty")) if isinstance(status, dict) else None
        price = _finite_number(status.get("filled_avg_price")) if isinstance(status, dict) else None
        if qty is None or price is None or qty <= 0 or price <= 0:
            return False
        if abs(qty - round(qty)) > 1e-9:
            return False
        if requested_qty is not None:
            try:
                requested_qty = float(requested_qty)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(requested_qty) or requested_qty < 1 or qty > requested_qty + 1e-9:
                return False
        return True

    def _poll_order_status(self, order_id: str, client_order_id: str = "") -> dict:
        """Poll a broker status endpoint until terminal or bounded timeout."""
        last = self._normalize_order_status({}, order_id=order_id, client_order_id=client_order_id)
        try:
            max_polls = max(1, int(self.ORDER_STATUS_MAX_POLLS))
        except (TypeError, ValueError):
            max_polls = 1
        try:
            timeout = max(0.0, float(self.ORDER_STATUS_POLL_TIMEOUT))
        except (TypeError, ValueError):
            timeout = 0.0
        try:
            interval = max(0.0, float(self.ORDER_STATUS_POLL_INTERVAL))
        except (TypeError, ValueError):
            interval = 0.0
        deadline = time.monotonic() + timeout

        for attempt in range(max_polls):
            try:
                current = self._order_status(order_id, client_order_id=client_order_id)
                if isinstance(current, dict):
                    # Adapters normally return normalized data; accepting a raw
                    # documented payload here keeps an unsafe adapter fail-closed.
                    if current.get("status") not in _ORDER_TERMINAL:
                        current = self._normalize_order_status(
                            current, order_id=order_id, client_order_id=client_order_id
                        )
                    last = current
                else:
                    last = self._normalize_order_status(
                        {}, order_id=order_id, client_order_id=client_order_id
                    )
            except Exception as exc:
                # A read failure is not evidence of a fill.  Keep polling within
                # the bound, then return unknown so the caller fails closed.
                log.warning(
                    "[%s] Order status read failed id=%s attempt=%d: %s",
                    self.name, str(order_id)[:24], attempt + 1, exc,
                )
                last = self._normalize_order_status(
                    {}, order_id=order_id, client_order_id=client_order_id
                )

            state = last.get("status")
            if state in _ORDER_TERMINAL and state != ORDER_STATUS_FILLED:
                return last
            if state == ORDER_STATUS_FILLED and self._fill_is_valid(last):
                return last
            if attempt + 1 >= max_polls or time.monotonic() >= deadline:
                break
            remaining = max(0.0, deadline - time.monotonic())
            delay = min(interval, remaining)
            if delay > 0:
                time.sleep(delay)
        # A broker that keeps the order pending beyond the bounded read window
        # is not a fill.  Preserve a distinct timeout for logs/diagnostics.
        if last.get("status") == ORDER_STATUS_PENDING:
            last = dict(last)
            last["status"] = ORDER_STATUS_TIMEOUT
        return last

    @staticmethod
    def _unsafe_order_result(
        order_id: str, symbol: str, requested_qty: int, client_order_id: str, status: str
    ) -> dict:
        """Preserve correlation while making an unconfirmed result non-fillable."""
        return {
            "id": str(order_id),
            "symbol": symbol,
            "qty": 0,
            "filled_avg_price": None,
            "status": status if status in _ORDER_TERMINAL else "unsafe",
            "order_status": status,
            "requested_qty": int(requested_qty),
            "client_order_id": client_order_id,
            "unsafe": True,
        }

    def _confirm_order(
        self, order_id: str, symbol: str, side: str, qty: int, client_order_id: str = ""
    ) -> Optional[dict]:
        """Turn a submitted order id into a fill only after safe confirmation."""
        order_id = str(order_id or "").strip()
        client_order_id = str(client_order_id or "").strip()
        if not order_id:
            log.error("[%s] Broker accepted %s %s but returned no order id", self.name, side, symbol)
            return None

        status = self._poll_order_status(order_id, client_order_id=client_order_id)
        state = status.get("status", ORDER_STATUS_UNKNOWN) if isinstance(status, dict) else ORDER_STATUS_UNKNOWN
        returned_id = str(status.get("id") or "").strip() if isinstance(status, dict) else ""
        returned_cid = str(status.get("client_order_id") or "").strip() if isinstance(status, dict) else ""
        if (returned_id and returned_id != order_id) or (
                client_order_id and returned_cid and returned_cid != client_order_id):
            log.error(
                "[%s] Order status correlation mismatch id=%s; refusing to book",
                self.name, order_id[:24],
            )
            return self._unsafe_order_result(
                order_id, symbol, qty, client_order_id, ORDER_STATUS_UNKNOWN
            )
        if state != ORDER_STATUS_FILLED:
            log.warning(
                "[%s] Order not filled id=%s state=%s; no local position booked",
                self.name, order_id[:24], state,
            )
            return self._unsafe_order_result(order_id, symbol, qty, client_order_id, state)
        if not self._fill_is_valid(status, requested_qty=qty):
            log.error(
                "[%s] Filled status lacked a valid quantity/price id=%s; no local position booked",
                self.name, order_id[:24],
            )
            return self._unsafe_order_result(
                order_id, symbol, qty, client_order_id, ORDER_STATUS_UNKNOWN
            )

        filled_qty = int(round(status["filled_qty"]))
        filled_price = float(status["filled_avg_price"])
        return {
            "id": order_id,
            "symbol": symbol,
            "qty": filled_qty,
            "filled_avg_price": filled_price,
            "status": ORDER_STATUS_FILLED,
            "order_status": status.get("broker_status"),
            "client_order_id": client_order_id,
        }

    def _validate_order_result(self, result: dict, requested_qty: Optional[int] = None) -> bool:
        """Only allow confirmed fills through BaseBroker's public methods."""
        if not isinstance(result, dict):
            return False
        try:
            qty = float(result.get("qty", 0) or 0)
        except (TypeError, ValueError):
            return False
        # A strict position lookup can report an already-flat account without
        # placing an order.  That is safe and remains compatible with callers.
        if qty == 0 and not str(result.get("id", "")).strip():
            return True
        if result.get("status") != ORDER_STATUS_FILLED:
            return False
        if not str(result.get("id", "")).strip() or not self._fill_is_valid({
            "filled_qty": qty,
            "filled_avg_price": result.get("filled_avg_price"),
        }, requested_qty=requested_qty):
            return False
        return True

    def _flatten_position(self, symbol: str) -> Optional[dict]:
        """Submit one correlated closing order for the current net position."""
        pos = self._get_position_strict(symbol)
        try:
            net_qty = float(pos.get("qty", 0) or 0) if pos else 0.0
        except (TypeError, ValueError):
            raise RuntimeError(f"invalid position quantity for {symbol}")
        if not math.isfinite(net_qty):
            raise RuntimeError(f"invalid position quantity for {symbol}")
        if net_qty == 0:
            return {"symbol": symbol, "qty": 0}
        requested_qty = _whole_quantity(abs(net_qty))
        if requested_qty is None:
            raise RuntimeError(f"non-whole position quantity for {symbol}")
        result = self._place_entry(
            symbol,
            "sell" if net_qty > 0 else "buy",
            requested_qty,
            client_order_id=self._new_client_order_id(),
        )
        try:
            filled_qty = float(result.get("qty", 0) or 0) if isinstance(result, dict) else 0.0
        except (TypeError, ValueError):
            filled_qty = 0.0
        # A terminal order that only filled part of a flatten request cannot
        # close the local trade safely; leave it explicitly unsafe so the caller
        # keeps monitoring the residual position.
        if isinstance(result, dict) and (
                result.get("status") == ORDER_STATUS_FILLED
                and (not math.isfinite(filled_qty) or int(filled_qty) != requested_qty)):
            filled_display = int(filled_qty) if math.isfinite(filled_qty) else "invalid"
            log.error(
                "[%s] Flatten for %s filled %s/%s; refusing to mark it flat",
                self.name, symbol, filled_display, requested_qty,
            )
            unsafe = dict(result)
            unsafe.update(status="unsafe", qty=0, filled_avg_price=None, unsafe=True)
            return unsafe
        return result

    def _sell_qty(self, symbol: str, qty: int) -> Optional[dict]:
        """Submit a correlated, confirmed partial close for the net position."""
        pos = self._get_position_strict(symbol)
        try:
            net_qty = float(pos.get("qty", 0) or 0) if pos else 0.0
        except (TypeError, ValueError):
            return None
        if not math.isfinite(net_qty) or net_qty == 0:
            return None
        available_qty = _whole_quantity(abs(net_qty))
        if available_qty is None:
            return None
        close_qty = min(int(qty), available_qty)
        if close_qty < 1:
            return None
        return self._place_entry(
            symbol,
            "sell" if net_qty > 0 else "buy",
            close_qty,
            client_order_id=self._new_client_order_id(),
        )
