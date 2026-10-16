"""
fundamentals_engine.py — ★ Fundamental health & valuation screen
==================================================================
The scanner is otherwise 100% technical: it would happily buy a fragile,
debt-loaded company with collapsing margins if the chart looked good. This
engine adds the "is the business healthy / is the stock priced sanely" check
a human analyst would run, from Yahoo's quoteSummary (free, works for NSE):

  health(symbol)          → "strong" | "fine" | "fragile" | "unknown"
  is_viable(symbol)       → False ONLY for confirmed-fragile names (fail-open)
  edge_adjustment(symbol) → small ± tilt on signal edge (growth, margins,
                            analyst target upside), same shape as sentiment's

COST DISCIPLINE: callers must apply this only to the handful of final signal
candidates per scan, NOT the 500-symbol universe loop — results are cached
24h (fundamentals move quarterly); failed fetches only 15min so an outage
can't disarm the gate all day (mirrors EarningsFilter).
"""
import logging
import math
import time
from typing import Dict, Optional

from brokers.data_provider import quote_summary
from config import Config

log = logging.getLogger(__name__)

_MODULES = "financialData,defaultKeyStatistics,summaryDetail"


def _raw(node: Optional[dict], *keys):
    """Walk nested Yahoo dicts and unwrap the {'raw': x, 'fmt': ...} leaf."""
    cur = node
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if isinstance(cur, dict):
        cur = cur.get("raw")
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        return None
    return float(cur) if math.isfinite(float(cur)) else None


class FundamentalsEngine:
    def __init__(self):
        # symbol → (data_dict_or_None, fetched_at, ok)
        self._cache: Dict[str, tuple] = {}
        self._cache_ttl = 86400   # 24h — fundamentals change quarterly
        self._fail_ttl  = 900     # 15min — retry soon after an outage

    # ── FETCH ─────────────────────────────────────────────────────────────────
    def get(self, symbol: str) -> Optional[dict]:
        """Fundamental snapshot for a symbol, or None if unavailable."""
        if symbol in self._cache:
            data, at, ok = self._cache[symbol]
            if time.time() - at < (self._cache_ttl if ok else self._fail_ttl):
                return data
        data, ok = self._fetch(symbol)
        if not ok:
            log.warning(f"[Fundamentals] {symbol}: fetch failed — screen inactive, "
                        f"retry in {self._fail_ttl // 60}min")
        self._cache[symbol] = (data, time.time(), ok)
        return data

    def _fetch(self, symbol: str) -> tuple:
        """Returns (data_or_None, ok). ok=False means the FETCH failed
        (≠ 'Yahoo has no fundamentals for this name', which is ok=True)."""
        try:
            res = quote_summary(symbol, _MODULES)
            if not res:
                return None, True
            fin  = res.get("financialData") or {}
            summ = res.get("summaryDetail") or {}
            price = _raw(fin, "currentPrice")
            target = _raw(fin, "targetMeanPrice")
            data = {
                "revenue_growth":   _raw(fin, "revenueGrowth"),      # YoY, fraction
                "operating_margin": _raw(fin, "operatingMargins"),
                "profit_margin":    _raw(fin, "profitMargins"),
                "free_cashflow":    _raw(fin, "freeCashflow"),       # absolute ₹
                "total_debt":       _raw(fin, "totalDebt"),
                "total_cash":       _raw(fin, "totalCash"),
                "roe":              _raw(fin, "returnOnEquity"),
                "trailing_pe":      _raw(summ, "trailingPE"),
                "recommendation":   _raw(fin, "recommendationMean"), # 1=strong buy…5=sell
                "target_upside": ((target - price) / price
                                  if target and price and price > 0 else None),
            }
            if all(v is None for v in data.values()):
                return None, True   # nothing known — genuinely no data
            return data, True
        except Exception as e:
            log.debug(f"[Fundamentals] {symbol} fetch error: {e}")
            return None, False

    # ── HEALTH ────────────────────────────────────────────────────────────────
    def health(self, symbol: str) -> str:
        """One-word business health, analyst-style.
        fragile = BOTH operating margin and free cash flow confirmed negative —
        the business is losing money on operations AND burning cash. Conservative
        on purpose: one missing/negative metric alone never blocks a trade."""
        d = self.get(symbol)
        if not d:
            return "unknown"
        om, fcf, growth = d["operating_margin"], d["free_cashflow"], d["revenue_growth"]
        if om is not None and om < 0 and fcf is not None and fcf < 0:
            return "fragile"
        if (growth is not None and growth > 0.10
                and om is not None and om > 0.10
                and (fcf is None or fcf > 0)):
            return "strong"
        return "fine"

    def is_viable(self, symbol: str) -> bool:
        """Gate: block only confirmed-fragile names. Unknown/missing data
        fails OPEN — a data outage must not halt trading."""
        return self.health(symbol) != "fragile"

    # ── EDGE TILT ─────────────────────────────────────────────────────────────
    def edge_adjustment(self, symbol: str) -> float:
        """± up to 0.3% edge tilt from growth, margin and analyst target upside.
        Small by design: fundamentals refine ranking among technical candidates,
        they don't overrule the technical signal."""
        d = self.get(symbol)
        if not d:
            return 0.0
        score = 0.0
        if d["revenue_growth"] is not None:
            score += max(-1.0, min(1.0, d["revenue_growth"] / 0.25))   # ±25% growth saturates
        if d["operating_margin"] is not None:
            score += max(-1.0, min(1.0, (d["operating_margin"] - 0.08) / 0.15))
        if d["target_upside"] is not None:
            score += max(-1.0, min(1.0, d["target_upside"] / 0.30))    # ±30% upside saturates
        return round(max(-1.0, min(1.0, score / 3.0)) * 0.003, 5)      # → ±0.3%
