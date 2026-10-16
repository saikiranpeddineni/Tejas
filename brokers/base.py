"""
brokers/base.py — Common broker surface for the Indian market (NSE equity).
===========================================================================
BaseBroker provides the *exact* public methods the rest of Tejas calls on a
broker (the old AlpacaClient surface), implemented once in terms of a small set
of low-level hooks each concrete broker fills in:

    _fetch_bars(symbol, timeframe, limit, start) -> List[Bar]
    _fetch_quote(symbol)                         -> Optional[Quote]
    _account_info()                              -> dict   (cash, portfolio_value, buying_power)
    _place_entry(symbol, side, qty, client_order_id) -> dict (must contain "id")
    _flatten(symbol)                             -> dict
    _positions()                                 -> List[dict]

Everything market-structure-specific to India is shared here:

  • Trading hours: NSE equity is 09:15–15:30 IST, Mon–Fri (minus holidays).
    We compute open/close locally from the IST wall clock — no broker /clock
    endpoint needed, which keeps every adapter tiny.
  • Orders are sized in *whole shares* (no fractional shares on NSE). The app
    still thinks in notional (₹); we convert notional → qty here.
  • There is no universal "bracket" order across Indian brokers for delivery
    equity, so place_bracket_order places a plain market entry and the bot's
    ExitEngine manages the stop-loss / take-profit in software (it already polls
    every cycle). This is broker-agnostic and matches how tejas.py monitors.
"""
import logging
import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from config import Config
from models import Bar, Quote

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# NSE equity session (IST)
SESSION_OPEN  = (9, 15)
SESSION_CLOSE = (15, 30)
SESSION_MINUTES = (SESSION_CLOSE[0] * 60 + SESSION_CLOSE[1]) - (SESSION_OPEN[0] * 60 + SESSION_OPEN[1])  # 375

# NSE trading holidays (extend as needed). Weekends handled separately.
NSE_HOLIDAYS = {
    # 2026 (partial — update from the official NSE calendar)
    "2026-01-26", "2026-03-06", "2026-03-25", "2026-04-01", "2026-04-03",
    "2026-04-14", "2026-05-01", "2026-08-15", "2026-10-02", "2026-11-09",
    "2026-12-25",
}


class BaseBroker:
    """Alpaca-compatible broker surface. Subclasses implement the low-level hooks."""

    name = "base"

    def __init__(self):
        self._quote_cache: Dict[str, Tuple[Quote, float]] = {}
        # Reconciliation must distinguish a genuinely flat account from a
        # failed position fetch.  The public API still returns [] for backward
        # compatibility, while this flag carries the health bit for callers
        # that need to fail closed.
        self._last_positions_ok = False

    # ════════════════════════════════════════════════════════════════════════
    # LOW-LEVEL HOOKS — subclasses must implement
    # ════════════════════════════════════════════════════════════════════════
    def _fetch_bars(self, symbol: str, timeframe: str, limit: int,
                    start: Optional[str] = None) -> List[Bar]:
        raise NotImplementedError

    def _fetch_quote(self, symbol: str) -> Optional[Quote]:
        raise NotImplementedError

    def _account_info(self) -> dict:
        raise NotImplementedError

    def _place_entry(self, symbol: str, side: str, qty: int, client_order_id: str = "") -> Optional[dict]:
        raise NotImplementedError

    def _flatten(self, symbol: str) -> Optional[dict]:
        raise NotImplementedError

    def _positions(self) -> List[dict]:
        raise NotImplementedError

    # Optional bulk hooks — default to per-symbol loops.
    def _fetch_bars_bulk(self, symbols: List[str], timeframe: str,
                         limit: int) -> Dict[str, List[Bar]]:
        out: Dict[str, List[Bar]] = {}
        for s in symbols:
            bars = self._fetch_bars(s, timeframe, limit)
            if bars:
                out[s] = bars
        return out

    def _fetch_quotes_bulk(self, symbols: List[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        for s in symbols:
            q = self._fetch_quote(s)
            if q:
                out[s] = q
        return out

    # ════════════════════════════════════════════════════════════════════════
    # ACCOUNT
    # ════════════════════════════════════════════════════════════════════════
    def get_account(self) -> Optional[dict]:
        try:
            return self._account_info()
        except Exception as e:
            log.error(f"[{self.name}] Account fetch failed: {e}")
            return None

    def get_portfolio_value(self) -> float:
        """Live account equity, or 0.0 if it cannot be determined.

        ★ Critically, on an account/auth failure this returns 0.0 — NOT
        Config.BANKROLL. Fabricating equity from the configured bankroll made a
        broker that couldn't even authenticate keep sizing and placing orders
        against a fake number. Callers must treat 0.0 as "cannot value account →
        halt", which tejas.py now does. (SimulatedBroker overrides this with its
        real paper cash+MTM, so paper mode is unaffected.)"""
        acc = self.get_account()
        if not acc:
            return 0.0
        if not isinstance(acc, dict):
            log.error(f"[{self.name}] Account endpoint returned an invalid payload")
            return 0.0
        try:
            value = float(acc.get("portfolio_value") or acc.get("cash") or 0.0)
        except (TypeError, ValueError):
            log.error(f"[{self.name}] Account returned a non-numeric portfolio value")
            return 0.0
        return value if math.isfinite(value) and value > 0 else 0.0

    # ════════════════════════════════════════════════════════════════════════
    # MARKET HOURS  (computed from IST wall clock — no API call)
    # ════════════════════════════════════════════════════════════════════════
    @staticmethod
    def _now_ist() -> datetime:
        return datetime.now(IST)

    @classmethod
    def _is_trading_day(cls, dt: datetime) -> bool:
        if dt.weekday() >= 5:                       # Sat/Sun
            return False
        return dt.strftime("%Y-%m-%d") not in NSE_HOLIDAYS

    def is_market_open(self) -> bool:
        if getattr(Config, "IGNORE_MARKET_HOURS", False):
            return True
        now = self._now_ist()
        if not self._is_trading_day(now):
            return False
        mins = now.hour * 60 + now.minute
        return (SESSION_OPEN[0] * 60 + SESSION_OPEN[1]) <= mins < (SESSION_CLOSE[0] * 60 + SESSION_CLOSE[1])

    def minutes_to_close(self) -> float:
        if not self.is_market_open():
            return -1.0
        now = self._now_ist()
        close = now.replace(hour=SESSION_CLOSE[0], minute=SESSION_CLOSE[1], second=0, microsecond=0)
        return max(0.0, (close - now).total_seconds() / 60)

    def minutes_since_open(self) -> float:
        if not self.is_market_open():
            return -1.0
        return max(0.0, SESSION_MINUTES - self.minutes_to_close())

    # ════════════════════════════════════════════════════════════════════════
    # QUOTES
    # ════════════════════════════════════════════════════════════════════════
    def get_quote(self, symbol: str, force: bool = False) -> Optional[Quote]:
        now = time.monotonic()
        if not force and symbol in self._quote_cache:
            q, ts = self._quote_cache[symbol]
            if now - ts < Config.QUOTE_CACHE_TTL:
                return q
        try:
            q = self._fetch_quote(symbol)
            if q is not None and not isinstance(q, Quote):
                log.warning(f"[{self.name}] Quote endpoint returned an invalid payload for {symbol}")
                return None
            if q is not None:
                try:
                    if not math.isfinite(float(q.mid)) or q.mid <= 0:
                        return None
                except (TypeError, ValueError):
                    return None
            if q:
                self._quote_cache[symbol] = (q, now)
            return q
        except Exception as e:
            log.warning(f"[{self.name}] Quote failed {symbol}: {e}")
            return None

    def get_quotes_bulk(self, symbols: List[str]) -> Dict[str, Quote]:
        if not symbols:
            return {}
        try:
            result = self._fetch_quotes_bulk(symbols)
            if not isinstance(result, dict):
                log.warning(f"[{self.name}] Bulk quote endpoint returned an invalid payload")
                return {}
            now = time.monotonic()
            valid = {}
            for symbol, quote in result.items():
                if not isinstance(symbol, str) or not isinstance(quote, Quote):
                    continue
                try:
                    mid = float(quote.mid)
                    spread = float(quote.spread_pct)
                except (TypeError, ValueError):
                    continue
                if (not math.isfinite(mid) or mid <= 0
                        or not math.isfinite(spread) or spread < 0):
                    continue
                valid[symbol] = quote
                self._quote_cache[symbol] = (quote, now)
            if len(valid) != len(result):
                log.warning(f"[{self.name}] Bulk quote payload dropped "
                            f"{len(result) - len(valid)} invalid quote(s)")
            return valid
        except Exception as e:
            log.warning(f"[{self.name}] Bulk quote failed: {e}")
            return {}

    # ════════════════════════════════════════════════════════════════════════
    # BARS
    # ════════════════════════════════════════════════════════════════════════
    def get_bars(self, symbol: str, timeframe: str = "1Day",
                 limit: int = 100, start: Optional[str] = None) -> List[Bar]:
        try:
            bars = self._fetch_bars(symbol, timeframe, limit, start)
            return bars if isinstance(bars, list) else []
        except Exception as e:
            log.warning(f"[{self.name}] Bars failed {symbol}/{timeframe}: {e}")
            return []

    def get_bars_bulk(self, symbols: List[str], timeframe: str = "1Day",
                      limit: int = 100) -> Dict[str, List[Bar]]:
        if not symbols:
            return {}
        try:
            bars = self._fetch_bars_bulk(symbols, timeframe, limit)
            return bars if isinstance(bars, dict) else {}
        except Exception as e:
            log.warning(f"[{self.name}] Bulk bars failed: {e}")
            return {}

    # ════════════════════════════════════════════════════════════════════════
    # ORDERS
    # ════════════════════════════════════════════════════════════════════════
    def _last_price(self, symbol: str, force: bool = True) -> float:
        q = self.get_quote(symbol, force=force)
        if q:
            try:
                mid = float(q.mid)
            except (TypeError, ValueError):
                mid = 0.0
            if math.isfinite(mid) and mid > 0:
                return mid
        bars = self.get_bars(symbol, "1Day", limit=2)
        try:
            close = float(bars[-1].close) if bars else 0.0
        except (TypeError, ValueError):
            return 0.0
        return close if math.isfinite(close) and close > 0 else 0.0

    def _notional_to_qty(self, symbol: str, notional: float, price: float = 0.0) -> int:
        try:
            notional = float(notional)
            price = float(price)
        except (TypeError, ValueError):
            return 0
        if not math.isfinite(notional) or notional <= 0:
            return 0
        if price <= 0:
            price = self._last_price(symbol)
        if not math.isfinite(price) or price <= 0:
            return 0
        return int(math.floor(notional / price))

    def place_market_order(self, symbol: str, side: str, notional: float) -> Optional[dict]:
        """Market order sized by notional (₹). Converts to whole shares."""
        side = str(side or "").strip().lower()
        try:
            notional = float(notional)
        except (TypeError, ValueError):
            log.warning(f"[{self.name}] Invalid order notional for {symbol}: {notional!r}")
            return None
        if side not in {"buy", "sell"}:
            log.warning(f"[{self.name}] Invalid order side for {symbol}: {side!r}")
            return None
        if not symbol or not math.isfinite(notional) or notional <= 0:
            log.warning(f"[{self.name}] Invalid market order: side={side!r} symbol={symbol!r} notional={notional!r}")
            return None
        if Config.DRY_RUN:
            log.info(f"[DRY RUN] {side.upper()} ₹{notional:.2f} of {symbol}")
            return {"dry_run": True, "id": f"dry_{int(time.time())}", "symbol": symbol}

        qty = self._notional_to_qty(symbol, notional)
        if qty < 1:
            log.warning(f"[{self.name}] {symbol}: notional ₹{notional:.0f} < 1 share — skipped")
            return None
        # Keep the identifier short enough for broker order-tag fields while
        # retaining enough entropy to correlate an ambiguous response.
        client_order_id = f"tj_{uuid.uuid4().hex[:16]}"
        try:
            res = self._place_entry(symbol, side.lower(), qty, client_order_id=client_order_id)
            if res:
                if not isinstance(res, dict) or not str(res.get("id", "")).strip():
                    # A 200 response without an order id is not a confirmed
                    # submission. Do not create a local trade that cannot be
                    # correlated or reconciled; the caller can retry after
                    # checking the broker's order book.
                    log.error(f"[{self.name}] Order response for {symbol} had no broker order id")
                    return None
                log.info(f"[{self.name}] Order: {side.upper()} {qty} {symbol} | id={str(res.get('id',''))[:12]} | cid={client_order_id}")
            return res
        except Exception as e:
            log.error(f"[{self.name}] Order failed {symbol}: {e}")
            return None

    def place_bracket_order(self, symbol: str, side: str, notional: float,
                            stop_loss: float, take_profit: float) -> Optional[dict]:
        """
        Entry = plain market order. SL/TP are managed in software by the bot's
        ExitEngine (no universal native bracket for NSE delivery equity).
        """
        if Config.DRY_RUN:
            log.info(f"[DRY RUN] BRACKET {side.upper()} ₹{notional:.2f} {symbol} | "
                     f"SL={stop_loss:.2f} TP={take_profit:.2f}")
            return {"dry_run": True, "id": f"dry_{int(time.time())}", "symbol": symbol}

        res = self.place_market_order(symbol, side, notional)
        if res:
            log.info(f"[{self.name}] Bracket entry {side.upper()} {symbol} | "
                     f"SL={stop_loss:.2f} TP={take_profit:.2f} (software-managed)")
        return res

    def close_position(self, symbol: str) -> Optional[dict]:
        if Config.DRY_RUN:
            log.info(f"[DRY RUN] CLOSE position: {symbol}")
            return {"dry_run": True, "symbol": symbol}
        try:
            result = self._flatten(symbol)
            if result is not None and not isinstance(result, dict):
                return None
            # Adapters use qty=0 to report an already-flat position. Any real
            # flatten submission must carry a broker order id; otherwise a
            # successful HTTP response could be mistaken for a filled exit.
            if (result and float(result.get("qty", 0) or 0) != 0 and
                    not str(result.get("id", "")).strip()):
                log.error(f"[{self.name}] Flatten response for {symbol} had no order id")
                return None
            return result
        except Exception as e:
            log.error(f"[{self.name}] Close position failed {symbol}: {e}")
            return None

    def close_position_partial(self, symbol: str, qty: int) -> Optional[dict]:
        """Sell part of a position (staged exit's first-target trim). Subclasses
        that support it implement `_sell_qty`; brokers without it return None and
        the execution engine simply skips trims (full SL/TP still work)."""
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return None
        if qty < 1:
            return None
        if Config.DRY_RUN:
            log.info(f"[DRY RUN] PARTIAL CLOSE {qty} of {symbol}")
            return {"dry_run": True, "symbol": symbol, "qty": qty}
        try:
            result = self._sell_qty(symbol, qty)
            if result is None or not isinstance(result, dict):
                return None
            if float(result.get("qty", qty) or 0) > 0 and not str(result.get("id", "")).strip():
                log.error(f"[{self.name}] Partial close response for {symbol} had no order id")
                return None
            return result
        except NotImplementedError:
            return None
        except Exception as e:
            log.error(f"[{self.name}] Partial close failed {symbol}: {e}")
            return None

    def _sell_qty(self, symbol: str, qty: int) -> Optional[dict]:
        raise NotImplementedError

    # ════════════════════════════════════════════════════════════════════════
    # POSITIONS
    # ════════════════════════════════════════════════════════════════════════
    def get_positions(self) -> List[dict]:
        try:
            positions = self._positions()
            if positions is None or not isinstance(positions, list):
                self._last_positions_ok = False
                log.warning(f"[{self.name}] Position endpoint returned an invalid payload")
                return []
            self._last_positions_ok = True
            return positions
        except Exception as e:
            self._last_positions_ok = False
            log.warning(f"[{self.name}] Positions fetch failed: {e}")
            return []

    def get_position(self, symbol: str) -> Optional[dict]:
        for p in self.get_positions():
            if p.get("symbol") == symbol:
                return p
        return None

    def _get_position_strict(self, symbol: str) -> Optional[dict]:
        """Look up a position without converting broker failures into 'flat'.

        ``get_positions()`` returns an empty list on a fetch failure for
        backward compatibility with scanners. Flattening has a stricter safety
        requirement: a failed position request must not be interpreted as no
        position, otherwise a local trade can be marked closed while exchange
        shares remain open and unprotected.
        """
        positions = self._positions()
        if not isinstance(positions, list):
            raise RuntimeError("position endpoint returned an invalid payload")
        for position in positions or []:
            if isinstance(position, dict) and position.get("symbol") == symbol:
                return position
        return None
