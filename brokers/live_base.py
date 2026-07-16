"""
brokers/live_base.py — Shared base for real Indian broker adapters.
===================================================================
Market DATA (bars/quotes, hence regime + features) comes from Yahoo by default,
so every live adapter only has to implement the trading-critical hooks:

    _account_info()         -> dict (cash, buying_power, portfolio_value)
    _place_entry(sym,side,qty) -> dict with "id"
    _flatten(symbol)        -> dict
    _positions()            -> List[dict]

⚠️  These adapters are written against each broker's published REST API but are
    UNTESTED without live credentials. Validate carefully (start with tiny
    quantities) before trusting real money. Auth tokens for most Indian brokers
    are short-lived (often regenerated daily via a login/TOTP flow) — see each
    adapter's docstring.
"""
import logging
from typing import Dict, List, Optional

import requests

from models import Bar, Quote
from brokers.base import BaseBroker
from brokers.data_provider import YahooDataProvider, make_session

log = logging.getLogger(__name__)


def _extract_error(body) -> Optional[str]:
    """Pull a human-readable broker error out of a 200-with-error or non-2xx body.
    Handles the differing conventions: AngelOne {status:false,message,errorcode},
    Zerodha/Upstox {status:"error",message,error_type} or {errors:[...]}."""
    if not isinstance(body, dict):
        return None
    st = body.get("status")
    is_err = (st is False) or (isinstance(st, str) and st.lower() in ("error", "false"))
    # Upstox: {"status":"error","errors":[{"message":...,"errorCode":...}]}
    errs = body.get("errors")
    if not is_err and not errs:
        return None
    if isinstance(errs, list) and errs:
        e0 = errs[0] if isinstance(errs[0], dict) else {}
        return str(e0.get("message") or e0.get("errorCode") or errs)
    return str(body.get("message") or body.get("error_type")
               or body.get("errorcode") or body.get("error") or body)


class LiveBrokerBase(BaseBroker):
    name = "live"

    def __init__(self):
        super().__init__()
        self.data = YahooDataProvider()
        self.session = make_session()   # pooled + retrying session

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
        return self._check(self.session.post(url, timeout=10, **kw))
