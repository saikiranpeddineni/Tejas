"""
feature_builder_gold.py — ★ UPGRADED: 37 Features (was 22, then 34)
====================================================================
NEW FEATURES ADDED:
  ★ Williams %R (overbought/oversold with speed)
  ★ Stochastic %K/%D (momentum turning points)
  ★ CCI (Commodity Channel Index — trend deviation)
  ★ On-Balance Volume (OBV) trend (smart money tracking)
  ★ Money Flow Index (volume-weighted RSI)
  ★ Distance from 52-week high/low (mean reversion anchor)
  ★ Price vs 200 EMA (major long-term trend)
  ★ Higher highs / lower lows pattern (trend structure)
  ★ Intraday range ratio (day's volatility vs ATR)
  ★ Bid-ask spread from live quote (execution cost signal)
  ★ Multi-timeframe score (combined daily + 1H signal)

TOTAL: 34 real features → significantly better ML discrimination
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

import db
from brokers.base import BaseBroker
from config import Config
from models import Bar, FeatureVector, Quote

log = logging.getLogger(__name__)

# Sector assignments (NSE) — 0:IT 1:Financials 2:Pharma 3:Consumer/FMCG
#                            4:Energy/Metals 5:index 6:other
SECTOR_MAP = {
    "TCS":0,"INFY":0,"HCLTECH":0,"WIPRO":0,"TECHM":0,"LTIM":0,
    "HDFCBANK":1,"ICICIBANK":1,"SBIN":1,"KOTAKBANK":1,"AXISBANK":1,
    "BAJFINANCE":1,"BAJAJFINSV":1,"INDUSINDBK":1,"SBILIFE":1,"HDFCLIFE":1,
    "SUNPHARMA":2,"DRREDDY":2,"CIPLA":2,"DIVISLAB":2,"APOLLOHOSP":2,
    "ITC":3,"HINDUNILVR":3,"NESTLEIND":3,"BRITANNIA":3,"TATACONSUM":3,
    "TITAN":3,"ASIANPAINT":3,"DMART":3,
    "RELIANCE":4,"ONGC":4,"NTPC":4,"POWERGRID":4,"COALINDIA":4,"BPCL":4,
    "TATASTEEL":4,"JSWSTEEL":4,"HINDALCO":4,"ADANIENT":4,"ADANIPORTS":4,
    "MARUTI":6,"TATAMOTORS":6,"M&M":6,"BAJAJ-AUTO":6,"EICHERMOT":6,"HEROMOTOCO":6,
    "ULTRACEMCO":6,"GRASIM":6,"LT":6,"BHARTIARTL":6,"UPL":6,
}
# Sector-strength proxy index per code (logical symbols resolved by the data layer).
SECTOR_ETFS = {0:"NIFTY",1:"BANKNIFTY",2:"NIFTY",3:"NIFTY",4:"NIFTY",5:"NIFTY",6:"NIFTY"}

FEATURE_NAMES = [
    # Original 22
    "price_vs_sma20","price_vs_ema9","ema9_vs_ema21",
    "rsi_14","momentum_5d","momentum_10d","momentum_20d","rate_of_change",
    "macd_histogram","macd_trend",
    "atr_pct","bb_position","bb_squeeze","volatility_20d",
    "volume_ratio","volume_trend","vwap_deviation",
    "spy_corr","relative_strength","sector_strength",
    "sector_code","candle_stability",   # 22: was "reserved" — same slot, old models still load
    # ★ New 12 gold features
    "williams_r",          # 23: Williams %R (0=overbought, -100=oversold)
    "stoch_k",             # 24: Stochastic %K
    "stoch_d",             # 25: Stochastic %D
    "cci",                 # 26: Commodity Channel Index (normalized)
    "obv_trend",           # 27: On-Balance Volume 10d slope
    "mfi",                 # 28: Money Flow Index (vol-weighted RSI)
    "dist_52w_high",       # 29: distance from 52-week high (−=above, +=below)
    "dist_52w_low",        # 30: distance from 52-week low
    "price_vs_ema200",     # 31: price vs 200-day EMA (mega trend)
    "higher_highs",        # 32: fraction of last 5 days with higher high
    "intraday_range_ratio",# 33: today's range / ATR (1=normal, >1.5=expansion)
    "bid_ask_spread",      # 34: live spread pct (execution cost)
    # ★ Market-context 3 — the market's own state, date-aligned to the window so
    #   historical training rows see the market AS IT WAS on that day (no skew).
    "market_trend_20d",    # 35: NIFTY 20d momentum (don't fight the tape)
    "market_vol_20d",      # 36: NIFTY 20d realized vol (regime proxy the ML can use)
    "trend_alignment",     # 37: multi-timeframe trend agreement (0=all bearish, 1=all bullish)
]
N_FEATURES = len(FEATURE_NAMES)  # 37


# ─────────────────────────────────────────────────────────────────────────────
# TECHNICAL HELPERS (pure numpy)
# ─────────────────────────────────────────────────────────────────────────────
def _sma(p, n):
    r = np.full_like(p, np.nan)
    for i in range(n-1, len(p)): r[i] = p[i-n+1:i+1].mean()
    return r

def _ema(p, n):
    r = np.full_like(p, np.nan)
    if len(p) < n:
        return r
    k = 2.0 / (n+1)
    # Seed from the first *finite* window. Seeding at a fixed index breaks when
    # the input has leading NaNs (e.g. the MACD line, which is NaN until the slow
    # EMA warms up): a NaN seed propagates through the whole recursion and silently
    # zeroes out macd_histogram / macd_trend for every bar.
    finite = np.where(np.isfinite(p))[0]
    if len(finite) < n:
        return r
    s = int(finite[0])
    seed = s + n - 1
    if seed >= len(p):
        return r
    r[seed] = p[s:seed+1].mean()
    for i in range(seed+1, len(p)): r[i] = p[i]*k + r[i-1]*(1-k)
    return r

def _rsi(p, n=14):
    d = np.diff(p); r = np.full(len(p), np.nan)
    if len(d) < n: return r
    g = np.where(d>0, d, 0.); lo = np.where(d<0, -d, 0.)
    ag, al = g[:n].mean(), lo[:n].mean()
    for i in range(n, len(d)):
        ag = (ag*(n-1)+g[i])/n; al = (al*(n-1)+lo[i])/n
        rs = ag/al if al > 0 else np.inf
        r[i+1] = 100-(100/(1+rs))
    return r / 100.

def _macd(p, f=12, s=26, sig=9):
    ef, es = _ema(p,f), _ema(p,s)
    ml = ef - es; sl = _ema(ml, sig)
    return ml, sl, ml-sl

def _atr(h, l, c, n=14):
    r = np.full(len(c), np.nan); tr = np.zeros(len(c))
    for i in range(1, len(c)):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    if len(tr) >= n:
        r[n] = tr[1:n+1].mean()
        for i in range(n+1, len(c)): r[i] = (r[i-1]*(n-1)+tr[i])/n
    return r

def _bb(p, n=20, s=2.0):
    mid = _sma(p, n)
    std = np.full_like(p, np.nan)
    for i in range(n-1, len(p)): std[i] = p[i-n+1:i+1].std()
    return mid+s*std, mid, mid-s*std

def _safe(arr, default=0.0):
    v = arr[~np.isnan(arr)]
    return float(v[-1]) if len(v) > 0 else default

def _williams_r(h, l, c, n=14):
    """Williams %R: 0=overbought, -100=oversold. Normalized to 0-1."""
    r = np.full(len(c), np.nan)
    for i in range(n-1, len(c)):
        hh = h[i-n+1:i+1].max(); ll = l[i-n+1:i+1].min()
        if hh - ll > 0:
            r[i] = ((hh - c[i]) / (hh - ll)) * -100
    return r / -100.  # normalize: 0=overbought (price at high), 1=oversold (price at low)

def _stochastic(h, l, c, k=14, d=3):
    """Stochastic %K and %D. Both normalized 0-1.
    NaN %K bars (warm-up, or a flat 14-bar window e.g. circuit-locked stock)
    are filled with NEUTRAL 0.5 before the %D average — nan_to_num's 0 read as
    'maximum oversold' and contradicted %K's own 0.5 fallback."""
    k_arr = np.full(len(c), np.nan)
    for i in range(k-1, len(c)):
        hh = h[i-k+1:i+1].max(); ll = l[i-k+1:i+1].min()
        if hh - ll > 0: k_arr[i] = (c[i] - ll) / (hh - ll)
    d_arr = _sma(np.where(np.isnan(k_arr), 0.5, k_arr), d)
    return k_arr, d_arr

def _cci(h, l, c, n=20):
    """CCI normalized to [-1, 1] range."""
    r = np.full(len(c), np.nan)
    tp = (h + l + c) / 3
    for i in range(n-1, len(c)):
        window = tp[i-n+1:i+1]
        ma = window.mean()
        md = np.abs(window - ma).mean()
        if md > 0: r[i] = (tp[i] - ma) / (0.015 * md)
    return np.clip(r / 200., -1., 1.)  # normalize to [-1, 1]

def _obv(c, v):
    """On-Balance Volume."""
    obv = np.zeros(len(c))
    for i in range(1, len(c)):
        if c[i] > c[i-1]: obv[i] = obv[i-1] + v[i]
        elif c[i] < c[i-1]: obv[i] = obv[i-1] - v[i]
        else: obv[i] = obv[i-1]
    return obv

def _mfi(h, l, c, v, n=14):
    """Money Flow Index (volume-weighted RSI), normalized 0-1."""
    tp = (h + l + c) / 3
    mf = tp * v
    r  = np.full(len(c), np.nan)
    for i in range(n, len(c)):
        # Window is the n periods ending at bar i. Use range(i-n+1, i+1) so the
        # current bar is included and tp[j-1] never wraps to tp[-1] (the old
        # range(i-n, i) made the first window compare tp[0] with tp[-1] — a
        # look-ahead onto the most recent bar — and dropped the current bar).
        pmf = sum(mf[j] for j in range(i-n+1, i+1) if tp[j] > tp[j-1])
        nmf = sum(mf[j] for j in range(i-n+1, i+1) if tp[j] < tp[j-1])
        if nmf > 0: r[i] = 100 - (100 / (1 + pmf/nmf))
        else: r[i] = 100.
    return r / 100.

def _bars_to_arrays(bars):
    c = np.array([b.close  for b in bars], dtype=np.float64)
    h = np.array([b.high   for b in bars], dtype=np.float64)
    l = np.array([b.low    for b in bars], dtype=np.float64)
    o = np.array([b.open   for b in bars], dtype=np.float64)
    v = np.array([b.volume for b in bars], dtype=np.float64)
    vw= np.array([b.vwap   for b in bars], dtype=np.float64)
    return c, h, l, o, v, vw


# ─────────────────────────────────────────────────────────────────────────────
# GOLD FEATURE BUILDER
# ─────────────────────────────────────────────────────────────────────────────
class FeatureBuilderGold:
    def __init__(self, broker: BaseBroker):
        self.broker = broker
        self._spy_cache: Optional[Tuple[List[Bar], float]] = None
        self._sector_cache: Dict[str, Tuple[List[Bar], float]] = {}
        self._hourly_cache: Dict[str, Tuple[List[Bar], float]] = {}

    def build(
        self,
        symbol: str,
        bars: List[Bar],          # daily bars
        quote: Optional[Quote] = None,
        trade_id: Optional[str] = None,
    ) -> Optional[FeatureVector]:
        if len(bars) < 30:
            return None

        c, h, l, o, v, vw = _bars_to_arrays(bars)
        price = float(quote.mid) if quote and quote.mid > 0 else float(c[-1])
        if price <= 0:
            return None

        # ── Original 22 features ──────────────────────────────────────────
        sma20 = _sma(c, 20); ema9 = _ema(c, 9); ema21 = _ema(c, 21)
        price_vs_sma20 = (price - _safe(sma20)) / _safe(sma20) if _safe(sma20) > 0 else 0.
        price_vs_ema9  = (price - _safe(ema9))  / _safe(ema9)  if _safe(ema9)  > 0 else 0.
        ema9_vs_ema21  = (_safe(ema9) - _safe(ema21)) / _safe(ema21) if _safe(ema21) > 0 else 0.

        def mom(n): return float((c[-1]-c[-n-1])/c[-n-1]) if len(c) > n and c[-n-1]>0 else 0.

        rsi14     = _safe(_rsi(c, 14), 0.5)
        mom5, mom10, mom20 = mom(5), mom(10), mom(20)
        macd_l, sig_l, hist = _macd(c)
        macd_norm = _safe(hist) / price if price > 0 else 0.
        hist_arr  = hist[~np.isnan(hist)]
        macd_trend= float(np.sign(hist_arr[-1]-hist_arr[-2])) if len(hist_arr)>=2 else 0.

        atr_arr = _atr(h, l, c, 14); atr_pct = _safe(atr_arr) / price if price>0 else 0.
        bbu, bbm, bbl = _bb(c); bb_u, bb_l, bb_m = _safe(bbu), _safe(bbl), _safe(bbm)
        bb_pos  = (price-bb_l)/(bb_u-bb_l) if bb_u>bb_l else 0.5
        bb_sq   = (bb_u-bb_l)/bb_m if bb_m>0 else 0.

        vol20d  = float(np.std(np.diff(np.log(c[-21:]))) * np.sqrt(252)) if len(c)>=21 else 0.
        vm      = min(20, len(v)-1); avg_v = v[-vm-1:-1].mean() if vm>0 else v.mean()
        vol_ratio = float(v[-1]/avg_v) if avg_v > 0 else 1.
        vol_trend = float((v[-6:-1][-1]-v[-6:-1][0])/(v[-6:-1][0]+1e-9)) if len(v)>=6 else 0.

        today_vwap = float(vw[-1]) if vw[-1]>0 else price
        vwap_dev   = (price-today_vwap)/today_vwap if today_vwap>0 else 0.

        spy_corr, rel_str, mkt_trend, mkt_vol = self._market_context(
            symbol, c, bars[-1].timestamp)
        sector_code  = SECTOR_MAP.get(symbol, 6)
        sec_strength = self._sector_strength(sector_code, bars[-1].timestamp)

        # ── ★ New 12 Gold Features ────────────────────────────────────────
        wr     = _safe(_williams_r(h, l, c, 14), 0.5)
        stk_k, stk_d = _stochastic(h, l, c, 14, 3)
        stk_k  = _safe(stk_k, 0.5); stk_d = _safe(stk_d, 0.5)
        cci_v  = _safe(_cci(h, l, c, 20), 0.0)
        
        # OBV trend: normalized slope over last 10 bars
        if len(c) >= 12:
            obv_arr = _obv(c, v)
            obv_slope = (obv_arr[-1] - obv_arr[-11]) / (abs(obv_arr[-11])+1e-9)
            obv_slope = float(np.clip(obv_slope, -2., 2.))
        else:
            obv_slope = 0.0

        mfi_v = _safe(_mfi(h, l, c, v, 14), 0.5)

        # 52-week distances
        bars_52w = min(252, len(c))
        high52w  = float(h[-bars_52w:].max()) if bars_52w > 0 else price
        low52w   = float(l[-bars_52w:].min()) if bars_52w > 0 else price
        dist_high= (high52w - price) / high52w if high52w > 0 else 0.   # 0=at high, 1=far below
        dist_low = (price - low52w)  / price   if price > 0 else 0.     # 0=at low, 1=far above

        # 200 EMA distance
        if len(c) >= 200:
            ema200  = _safe(_ema(c, 200))
            p_vs_200= (price - ema200) / ema200 if ema200 > 0 else 0.
        else:
            p_vs_200 = 0.0

        # Higher highs pattern (last 5 bars)
        hh_count = sum(1 for i in range(-4, 0) if i > -len(h) and h[i] > h[i-1]) / 4.

        # Intraday range vs ATR
        today_range = float(h[-1] - l[-1])
        atr_today   = _safe(atr_arr)
        intra_ratio = today_range / atr_today if atr_today > 0 else 1.0

        # Live bid-ask spread
        spread = float(quote.spread_pct) if quote else 0.001

        # ★ Candle stability (GainzAlgo-inspired): body / full range, averaged
        #   over the last 3 bars. 1 = decisive full-bodied candles behind the
        #   move; ~0 = doji-like indecision (signals off those are weaker).
        stab = []
        for i in range(max(-3, -len(c)), 0):
            rng = h[i] - l[i]
            if rng > 0:
                stab.append(abs(c[i] - o[i]) / rng)
        cstab = float(np.mean(stab)) if stab else 0.5

        # ★ Multi-timeframe trend alignment: how many independent trend reads
        #   agree bullish (short EMA cross, price vs SMA20, MACD, 20d momentum,
        #   mega-trend vs EMA200 — neutral 0.5 when <200 bars of history).
        long_sig = (1.0 if p_vs_200 > 0 else 0.0) if len(c) >= 200 else 0.5
        trend_align = (float(price_vs_sma20 > 0) + float(ema9_vs_ema21 > 0)
                       + float(macd_norm > 0) + float(mom20 > 0) + long_sig) / 5.0

        return FeatureVector(
            trade_id=trade_id or str(uuid.uuid4())[:12],
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            # Original 22
            price_vs_sma20=float(np.clip(price_vs_sma20, -.5, .5)),
            price_vs_ema9 =float(np.clip(price_vs_ema9,  -.5, .5)),
            ema9_vs_ema21 =float(np.clip(ema9_vs_ema21,  -.2, .2)),
            rsi_14        =float(np.clip(rsi14, 0, 1)),
            momentum_5d   =float(np.clip(mom5,  -.3, .3)),
            momentum_10d  =float(np.clip(mom10, -.5, .5)),
            momentum_20d  =float(np.clip(mom20, -.6, .6)),
            rate_of_change=float(np.clip(mom10, -.5, .5)),
            macd_histogram=float(np.clip(macd_norm, -.05, .05)),
            macd_trend    =float(macd_trend),
            atr_pct       =float(np.clip(atr_pct, 0, .15)),
            bb_position   =float(np.clip(bb_pos, 0, 1)),
            bb_squeeze    =float(np.clip(bb_sq, 0, .3)),
            volatility_20d=float(np.clip(vol20d, 0, 2.)),
            volume_ratio  =float(np.clip(vol_ratio, 0, 10)),
            volume_trend  =float(np.clip(vol_trend, -2, 2)),
            vwap_deviation=float(np.clip(vwap_dev, -.1, .1)),
            spy_corr      =float(np.clip(spy_corr, -1, 1)),
            relative_strength=float(np.clip(rel_str, -2, 2)),
            sector_strength=float(np.clip(sec_strength, -.5, .5)),
            sector_code   =sector_code,
            candle_stability=float(np.clip(cstab, 0, 1)),
            # ★ New 12
            williams_r    =float(np.clip(wr, 0, 1)),
            stoch_k       =float(np.clip(stk_k, 0, 1)),
            stoch_d       =float(np.clip(stk_d, 0, 1)),
            cci           =float(np.clip(cci_v, -1, 1)),
            obv_trend     =float(np.clip(obv_slope, -2, 2)),
            mfi           =float(np.clip(mfi_v, 0, 1)),
            dist_52w_high =float(np.clip(dist_high, 0, 1)),
            dist_52w_low  =float(np.clip(dist_low, 0, 2)),
            price_vs_ema200=float(np.clip(p_vs_200, -.5, .5)),
            higher_highs  =float(hh_count),
            intraday_range_ratio=float(np.clip(intra_ratio, 0, 5)),
            bid_ask_spread=float(np.clip(spread, 0, .02)),
            # ★ Market context 3
            market_trend_20d=float(np.clip(mkt_trend, -.25, .25)),
            market_vol_20d  =float(np.clip(mkt_vol, 0, 1)),
            trend_alignment =float(trend_align),
        )

    def _market_context(self, symbol, c, last_ts=None):
        """Returns (index_corr, relative_strength, market_trend_20d, market_vol_20d).

        ★ Date-aligned: index bars are truncated to those on/before the feature
        window's last bar. The old count-based slice (`spy[-len(c):]`) compared a
        HISTORICAL stock window against TODAY'S index bars — every training row
        built by forecast_trainer saw the same live market context regardless of
        its date (train/serve skew on spy_corr/relative_strength). Now historical
        rows see the market as it was on their own day."""
        neutral = (0., 0., 0., 0.)
        if symbol == Config.MARKET_INDEX_SYMBOL: return 1.0, 0.0, 0.0, 0.0
        try:
            spy = self._get_spy_bars()
            if not spy: return neutral
            if last_ts is not None:
                cutoff = last_ts.date() if hasattr(last_ts, "date") else last_ts
                spy = [b for b in spy if b.timestamp.date() <= cutoff]
            if len(spy) < 21: return neutral
            sc = np.array([b.close for b in spy])
            n  = min(len(sc), len(c))
            sr = np.diff(np.log(sc[-21:])); tr = np.diff(np.log(c[-21:]))
            corr = float(np.corrcoef(tr, sr)[0,1]) if len(sr)>=2 and len(tr)>=2 and n>=21 else 0.
            spy_ret = float((sc[-1]-sc[-21])/sc[-21])
            stk_ret = float((c[-1]-c[-21])/c[-21]) if len(c) >= 21 else 0.
            mkt_vol = float(np.std(sr) * np.sqrt(252)) if len(sr) >= 2 else 0.
            return corr, stk_ret - spy_ret, spy_ret, mkt_vol
        except Exception: return neutral

    def _sector_strength(self, code, last_ts=None):
        """10d sector-proxy momentum, date-aligned to the feature window — the
        old always-live fetch gave every HISTORICAL training row today's sector
        momentum (a near-constant the model can't learn from) while live rows
        varied: the same train/serve skew _market_context had."""
        etf = SECTOR_ETFS.get(code, Config.MARKET_INDEX_SYMBOL)
        try:
            import time as t
            cached = self._sector_cache.get(etf)
            if cached and t.time()-cached[1] < 300:
                bars = cached[0]
            else:
                bars = self.broker.get_bars(etf, "1Day", limit=500)
                self._sector_cache[etf] = (bars, t.time())
            if last_ts is not None and bars:
                cutoff = last_ts.date() if hasattr(last_ts, "date") else last_ts
                bars = [b for b in bars if b.timestamp.date() <= cutoff]
            if bars and len(bars) >= 11:
                return float((bars[-1].close - bars[-11].close) / bars[-11].close)
        except Exception: pass
        return 0.

    def _get_spy_bars(self):
        import time as t
        now = t.time()
        if self._spy_cache and now - self._spy_cache[1] < 300:
            return self._spy_cache[0]
        # 500 bars (was 60): historical training replays windows up to ~420 bars
        # back, and date-aligned market context needs index history covering them.
        bars = self.broker.get_bars(Config.MARKET_INDEX_SYMBOL, "1Day", limit=500)
        if bars: self._spy_cache = (bars, now)
        return bars
