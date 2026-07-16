"""
brokers/simulated.py — Paper-trading broker on real NSE data.
=============================================================
Fetches *real* Indian market data (Yahoo) but fills orders against an in-memory
cash/position book. Nothing hits a real broker. This is the verified path you
can run today without any API credentials.

Two testing modes:
  • BROKER=simulated, DRY_RUN=true   → log-only, no fills, equity stays flat.
  • BROKER=simulated, DRY_RUN=false  → true paper trading: fills, positions and
                                       P&L are tracked in memory and equity moves.
                                       (Recommended for actually testing strategy.)
"""
import logging
import time
from typing import Dict, List, Optional

from config import Config
from models import Bar, Quote
from brokers.base import BaseBroker
from brokers.data_provider import YahooDataProvider

log = logging.getLogger(__name__)


class SimulatedBroker(BaseBroker):
    name = "simulated"

    def __init__(self):
        super().__init__()
        self.data = YahooDataProvider()
        self.cash = float(Config.BANKROLL)
        # symbol -> {"qty": int, "avg": float}
        self.book: Dict[str, dict] = {}
        self._bars_cache: Dict[str, tuple] = {}   # symbol -> (bars, ts)
        self._restore_from_db()
        log.info(f"[simulated] Paper broker ready | cash=₹{self.cash:,.0f} "
                 f"| holdings={len(self.book)} | data={self.data.name} | DRY_RUN={Config.DRY_RUN}")

    def _restore_from_db(self):
        """Rebuild the cash/holdings book from open trades persisted in a prior
        session so paper equity & positions carry over (nothing lost on restart)."""
        try:
            import db
            rows = db.fetchall(
                "SELECT symbol, shares, size_usd, entry_price "
                "FROM trades WHERE status='open'"
            )
            spent = 0.0
            for r in rows:
                qty = float(r["shares"] or 0)
                cost = float(r["size_usd"] or 0)
                avg = float(r["entry_price"] or 0)
                if qty <= 0 or avg <= 0:
                    continue
                self.book[r["symbol"]] = {"qty": qty, "avg": avg}
                spent += cost
            if self.book:
                self.cash = max(0.0, self.cash - spent)
                log.info(f"[simulated] Restored {len(self.book)} position(s) from previous session "
                         f"(₹{spent:,.0f} deployed)")
        except Exception as e:
            log.warning(f"[simulated] position restore skipped: {e}")

    BARS_TTL = 1800   # daily bars change once/day — cache 30 min

    # ── data hooks ──────────────────────────────────────────────────────────
    def _fetch_bars(self, symbol, timeframe="1Day", limit=100, start=None) -> List[Bar]:
        key = f"{symbol}:{timeframe}:{limit}"
        cached = self._bars_cache.get(key)
        if cached and time.monotonic() - cached[1] < self.BARS_TTL:
            return cached[0]
        bars = self.data.get_bars(symbol, timeframe, limit, start)
        if bars:
            self._bars_cache[key] = (bars, time.monotonic())
        return bars

    def _fetch_bars_bulk(self, symbols, timeframe="1Day", limit=100) -> Dict[str, List[Bar]]:
        """Serve cached symbols instantly; fetch only the misses, in parallel."""
        out, misses = {}, []
        now = time.monotonic()
        for s in symbols:
            c = self._bars_cache.get(f"{s}:{timeframe}:{limit}")
            if c and now - c[1] < self.BARS_TTL:
                out[s] = c[0]
            else:
                misses.append(s)
        if misses:
            fetched = self.data.get_bars_bulk(misses, timeframe, limit)
            t = time.monotonic()
            for s, bars in fetched.items():
                out[s] = bars
                self._bars_cache[f"{s}:{timeframe}:{limit}"] = (bars, t)
        return out

    def _fetch_quote(self, symbol) -> Optional[Quote]:
        return self.data.get_quote(symbol)

    def _fetch_quotes_bulk(self, symbols) -> Dict[str, Quote]:
        return self.data.get_quotes_bulk(symbols)

    # ── account / positions ───────────────────────────────────────────────────
    def _market_value(self) -> float:
        mv = 0.0
        for sym, pos in self.book.items():
            px = self._last_price(sym)
            mv += pos["qty"] * (px if px > 0 else pos["avg"])
        return mv

    def _account_info(self) -> dict:
        pv = self.cash + self._market_value()
        return {
            "cash": self.cash,
            "buying_power": self.cash,
            "portfolio_value": pv,
            "currency": "INR",
        }

    def _positions(self) -> List[dict]:
        out = []
        for sym, pos in self.book.items():
            px = self._last_price(sym) or pos["avg"]
            out.append({
                "symbol": sym,
                "qty": pos["qty"],
                "avg_entry_price": pos["avg"],
                "current_price": px,
                "market_value": pos["qty"] * px,
                "unrealized_pl": (px - pos["avg"]) * pos["qty"],
            })
        return out

    # ── order hooks (only used when DRY_RUN=false) ────────────────────────────
    def _place_entry(self, symbol, side, qty) -> Optional[dict]:
        px = self._last_price(symbol)
        if px <= 0:
            log.warning(f"[simulated] no price for {symbol} — order rejected")
            return None
        if side == "buy":
            cost = qty * px
            if cost > self.cash:
                qty = int(self.cash // px)
                if qty < 1:
                    log.warning(f"[simulated] insufficient cash for {symbol}")
                    return None
                cost = qty * px
            self.cash -= cost
            pos = self.book.get(symbol, {"qty": 0, "avg": 0.0})
            new_qty = pos["qty"] + qty
            pos["avg"] = (pos["avg"] * pos["qty"] + px * qty) / new_qty
            pos["qty"] = new_qty
            self.book[symbol] = pos
        else:  # sell to reduce/close
            pos = self.book.get(symbol)
            if not pos:
                return None
            qty = min(qty, pos["qty"])
            self.cash += qty * px
            pos["qty"] -= qty
            if pos["qty"] <= 0:
                self.book.pop(symbol, None)
        oid = f"sim_{int(time.time()*1000)}"
        log.info(f"[simulated] FILL {side.upper()} {qty} {symbol} @ ₹{px:.2f} "
                 f"| cash=₹{self.cash:,.0f}")
        return {"id": oid, "symbol": symbol, "qty": qty, "filled_avg_price": px}

    def _flatten(self, symbol) -> Optional[dict]:
        pos = self.book.get(symbol)
        if not pos:
            return {"symbol": symbol, "qty": 0}
        return self._place_entry(symbol, "sell", pos["qty"])

    def _sell_qty(self, symbol, qty) -> Optional[dict]:
        """Partial close (staged exit trim) — the sell path already handles
        reducing a position; this just exposes it with an explicit qty."""
        pos = self.book.get(symbol)
        if not pos or pos["qty"] < 1:
            return None
        return self._place_entry(symbol, "sell", min(int(qty), pos["qty"]))
