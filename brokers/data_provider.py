"""
brokers/data_provider.py — Free NSE market data via Yahoo Finance.
==================================================================
No API key, no extra dependency (uses `requests`, already required). Serves
daily/intraday OHLCV and a synthetic quote for NSE equities and indices.

Logical-symbol mapping (bot symbol -> Yahoo symbol):
    NIFTY     -> ^NSEI        (NIFTY 50 index)
    INDIAVIX  -> ^INDIAVIX    (India VIX)
    <STOCK>   -> <STOCK>.NS   (e.g. RELIANCE -> RELIANCE.NS)

Used by SimulatedBroker for paper/testing, and as a data fallback for live
adapters when their historical endpoint needs an instrument token you haven't
wired up yet.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None

from models import Bar, Quote

_MAX_WORKERS = 14   # parallel Yahoo fetches (keeps large-universe scans fast)

log = logging.getLogger(__name__)


def make_session(pool: int = _MAX_WORKERS + 4) -> requests.Session:
    """A requests session sized for our concurrency (avoids the 'Connection pool
    is full' churn) with automatic retry/backoff on transient 429/5xx."""
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    retry = None
    if Retry is not None:
        retry = Retry(total=3, backoff_factor=0.5,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=frozenset(["GET", "POST"]))
    adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# ─────────────────────────────────────────────────────────────────────────────
# SHARED YAHOO ACCESS (crumb + cookies) — used by earnings_filter & sentiment_engine
# Yahoo's quoteSummary endpoint (earnings dates) requires a cookie+crumb handshake.
# We keep one shared, lazily-refreshed session so the whole app makes the handshake
# once rather than per call.
# ─────────────────────────────────────────────────────────────────────────────
_YH_LOCK = threading.Lock()
_YH = {"session": None, "crumb": None, "ts": 0.0}
_YH_TTL = 6 * 3600   # refresh crumb every 6h


def _yahoo_ctx():
    """Return (session, crumb) with a valid crumb, refreshing if stale. crumb may
    be None if the handshake fails (callers that don't need a crumb still work)."""
    with _YH_LOCK:
        now = time.time()
        if _YH["session"] is not None and (now - _YH["ts"]) < _YH_TTL:
            return _YH["session"], _YH["crumb"]
        s = make_session()
        crumb = None
        try:
            s.get("https://fc.yahoo.com", timeout=8)
        except Exception:
            pass
        try:
            s.get("https://finance.yahoo.com", timeout=8)
        except Exception:
            pass
        try:
            r = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=8)
            txt = r.text.strip()
            if r.status_code == 200 and txt and "<" not in txt and len(txt) < 40:
                crumb = txt
        except Exception:
            pass
        _YH.update(session=s, crumb=crumb, ts=now)
        return s, crumb


def quote_summary(symbol: str, modules: str) -> Optional[dict]:
    """Yahoo quoteSummary for a single ysymbol (e.g. 'RELIANCE.NS'). Returns the
    first result dict, or None. Handles the crumb handshake transparently."""
    ysym = _yahoo_symbol(symbol)
    for attempt in range(2):
        s, crumb = _yahoo_ctx()
        params = {"modules": modules}
        if crumb:
            params["crumb"] = crumb
        try:
            r = s.get(
                f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ysym}",
                params=params, timeout=10,
            )
            if r.status_code == 401 and attempt == 0:
                with _YH_LOCK:        # force a crumb refresh and retry once
                    _YH["ts"] = 0.0
                continue
            r.raise_for_status()
            res = (r.json().get("quoteSummary", {}).get("result") or [])
            return res[0] if res else None
        except Exception as e:
            if attempt == 0:
                continue
            log.debug(f"[yahoo] quoteSummary failed {symbol}: {e}")
            return None
    return None


def search_news(query: str, count: int = 8) -> List[dict]:
    """Yahoo news search. Returns a list of {'title','publisher','link'} dicts.
    No crumb needed for the search endpoint."""
    s, _ = _yahoo_ctx()
    try:
        r = s.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": query, "newsCount": count, "quotesCount": 0,
                    "enableFuzzyQuery": "false"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("news", []) or []
    except Exception as e:
        log.debug(f"[yahoo] news search failed {query}: {e}")
        return []

_INDEX_MAP = {
    "NIFTY":    "^NSEI",
    "NIFTY50":  "^NSEI",
    "BANKNIFTY":"^NSEBANK",
    "INDIAVIX": "^INDIAVIX",
}

_TF_TO_INTERVAL = {
    "1Min": "1m", "5Min": "5m", "15Min": "15m", "30Min": "30m",
    "1Hour": "60m", "60Min": "60m", "1Day": "1d", "1Week": "1wk",
}


def _yahoo_symbol(symbol: str) -> str:
    s = symbol.upper().replace(" ", "")
    if s in _INDEX_MAP:
        return _INDEX_MAP[s]
    if s.startswith("^") or s.endswith(".NS") or s.endswith(".BO"):
        return s
    return f"{s}.NS"


def _range_for(interval: str, limit: int) -> str:
    if interval == "1d":
        if limit <= 25:  return "1mo"
        if limit <= 70:  return "3mo"
        if limit <= 130: return "6mo"
        if limit <= 260: return "1y"
        return "2y"
    if interval in ("60m", "30m", "15m", "5m", "1m"):
        return "5d" if limit <= 60 else "1mo"
    return "1y"


class YahooDataProvider:
    name = "yahoo"

    def __init__(self):
        self.session = make_session()

    def get_bars(self, symbol: str, timeframe: str = "1Day", limit: int = 100,
                 start: Optional[str] = None) -> List[Bar]:
        interval = _TF_TO_INTERVAL.get(timeframe, "1d")
        ysym = _yahoo_symbol(symbol)
        rng = _range_for(interval, limit)
        try:
            resp = self.session.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}",
                params={"range": rng, "interval": interval},
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json().get("chart", {}).get("result")
            if not result:
                return []
            r0 = result[0]
            ts = r0.get("timestamp") or []
            q = (r0.get("indicators", {}).get("quote") or [{}])[0]
            opens, highs = q.get("open", []), q.get("high", [])
            lows, closes = q.get("low", []), q.get("close", [])
            vols = q.get("volume", [])
            bars: List[Bar] = []
            for i in range(len(ts)):
                try:
                    c = closes[i]
                    if c is None:
                        continue
                    bars.append(Bar(
                        symbol=symbol,
                        timestamp=datetime.fromtimestamp(ts[i], tz=timezone.utc),
                        open=float(opens[i] if opens[i] is not None else c),
                        high=float(highs[i] if highs[i] is not None else c),
                        low=float(lows[i] if lows[i] is not None else c),
                        close=float(c),
                        volume=int(vols[i] or 0),
                        vwap=float(c),
                    ))
                except (IndexError, TypeError, ValueError):
                    continue
            return bars[-limit:] if limit else bars
        except Exception as e:
            log.warning(f"[yahoo] bars failed {symbol}: {e}")
            return []

    def get_quote(self, symbol: str) -> Optional[Quote]:
        ysym = _yahoo_symbol(symbol)
        try:
            resp = self.session.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}",
                params={"range": "1d", "interval": "1m"},
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json().get("chart", {}).get("result")
            if not result:
                return None
            meta = result[0].get("meta", {})
            price = float(meta.get("regularMarketPrice") or 0)
            if price <= 0:
                return None
            # ★ Real freshness: stamp the quote with the exchange's last trade time,
            #   NOT now(). Otherwise every stale/closed-market price looks "fresh"
            #   and downstream staleness gates are defeated.
            mt = meta.get("regularMarketTime")
            ts = (datetime.fromtimestamp(int(mt), tz=timezone.utc)
                  if mt else datetime.now(timezone.utc))
            half = price * 0.0005  # 0.05% synthetic spread
            return Quote(symbol=symbol, bid=price - half, ask=price + half,
                         bid_size=1, ask_size=1, timestamp=ts)
        except Exception as e:
            log.warning(f"[yahoo] quote failed {symbol}: {e}")
            return None

    def get_bars_bulk(self, symbols: List[str], timeframe: str = "1Day",
                      limit: int = 100) -> Dict[str, List[Bar]]:
        out: Dict[str, List[Bar]] = {}
        if not symbols:
            return out
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            for s, bars in ex.map(lambda x: (x, self.get_bars(x, timeframe, limit)), symbols):
                if bars:
                    out[s] = bars
        return out

    def get_quotes_bulk(self, symbols: List[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        if not symbols:
            return out
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            for s, q in ex.map(lambda x: (x, self.get_quote(x)), symbols):
                if q:
                    out[s] = q
        return out
