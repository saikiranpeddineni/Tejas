"""
universe.py — Dynamic NSE trading universe (no hand-picked list).
=================================================================
The tradable universe is pulled from the live NSE index constituents (e.g.
NIFTY 500 = the ~500 largest/most-liquid NSE stocks ≈ the investable market),
not a fixed favourites list. It refreshes once per day and is cached to disk.

NSE also gives each stock's Industry, so sectors are assigned from NSE's own
classification (used by the risk manager's sector caps).

Config.UNIVERSE selects the breadth:
    nifty50 | nifty100 | nifty200 | nifty500   (default nifty500)
    watchlist  → fall back to the static Config.WATCHLIST
"""
import csv
import json
import logging
import os
import time
import urllib.request

from config import Config

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE_DIR, "universe.local.json")

_CSV = {
    "nifty50":  "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "nifty100": "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    "nifty200": "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "nifty500": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
}

# in-memory cache for the current day
_mem = {"date": None, "index": None, "symbols": [], "sectors": {}}


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _industry_code(industry: str) -> int:
    """Map an NSE Industry label to the bot's sector code (0..6)."""
    i = industry.lower()
    if "information technology" in i:                          return 0
    if "financial" in i or "bank" in i:                        return 1
    if "healthcare" in i or "pharma" in i:                     return 2
    if any(w in i for w in ("consumer", "fast moving", "fmcg", "textile")): return 3
    if any(w in i for w in ("oil", "gas", "metal", "mining", "power", "energy", "coal")): return 4
    return 6  # other (capital goods, services, telecom, realty, auto, etc.)


def _fetch(index: str):
    req = urllib.request.Request(_CSV[index], headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        text = r.read().decode("utf-8", "replace")
    symbols, sectors = [], {}
    # Use csv.reader (not str.split) — NSE company names can contain commas
    # ("Company A, B & Co. Ltd."), which a naive split shifts, misaligning the
    # Symbol/Series columns and dropping or mis-sectoring valid stocks.
    rows = list(csv.reader(text.splitlines()))
    for parts in rows[1:]:                         # skip header
        if len(parts) < 4:
            continue
        industry, sym, series = parts[1].strip(), parts[2].strip(), parts[3].strip()
        if not sym or series.upper() != "EQ":
            continue
        symbols.append(sym)
        sectors[sym] = _industry_code(industry)
    return symbols, sectors


def get_universe():
    """Return (symbols, {symbol: sector_code}) for the configured index."""
    index = getattr(Config, "UNIVERSE", "nifty500").lower()
    if index in ("watchlist", "static"):
        return list(Config.WATCHLIST), {}

    if _mem["date"] == _today() and _mem["index"] == index and _mem["symbols"]:
        return _mem["symbols"], _mem["sectors"]

    # disk cache (avoids re-downloading within the same day / across restarts)
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == _today() and d.get("index") == index and d.get("symbols"):
            _mem.update(date=_today(), index=index, symbols=d["symbols"], sectors=d.get("sectors", {}))
            return d["symbols"], d.get("sectors", {})
    except Exception:
        pass

    try:
        symbols, sectors = _fetch(index)
        cap = int(getattr(Config, "UNIVERSE_MAX", 500))
        symbols = symbols[:cap]
        sectors = {s: sectors.get(s, 6) for s in symbols}
        _mem.update(date=_today(), index=index, symbols=symbols, sectors=sectors)
        try:
            with open(CACHE, "w", encoding="utf-8") as f:
                json.dump({"date": _today(), "index": index, "symbols": symbols, "sectors": sectors}, f)
        except Exception:
            pass
        log.info(f"[Universe] {index}: {len(symbols)} stocks from live NSE constituents")
        return symbols, sectors
    except Exception as e:
        log.warning(f"[Universe] NSE fetch failed ({e}) — falling back to static WATCHLIST")
        return list(Config.WATCHLIST), {}
