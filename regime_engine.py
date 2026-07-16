"""
regime_engine.py — Market Regime Detection
===========================================
REGIMES:
  bull_low_vol   → VIX < 15,  trend up    → full size
  bull_high_vol  → VIX 15-25, trend up    → 80% size
  bear_low_vol   → VIX < 25,  trend down  → 50% size
  bear_high_vol  → VIX 25-35, trend down  → 25% size
  range_low_vol  → VIX < 15,  sideways    → 90% size
  range_high_vol → VIX 15-35, sideways    → 40% size
  high_fear      → VIX 35-45             → 10% size  (small positions allowed)
  extreme_fear   → VIX > 45              → 0% (true halt — 2020-crash level only)

VIX ESTIMATION:
  Primary:   SPY 10-day realized vol (annualized × 100) — fast, accurate
  Secondary: SPY 21-day realized vol (smoothed)
  Combined:  60% short / 40% long window blend
  Fallback:  VXX price × 0.85 (rough empirical fit, VXX decays so coeff < 1)
  Absolute fallback: 20.0 (safe middle ground)

  This is far more accurate than the old vxx_price * 1.1 which mapped
  VXX=$40 → VIX proxy=44, incorrectly triggering extreme_fear when VIX was ~37.
"""
import logging
import time
from typing import Dict, Optional, Tuple

import numpy as np

from brokers.base import BaseBroker
from config import Config

log = logging.getLogger(__name__)


class RegimeEngine:
    def __init__(self, broker: BaseBroker):
        self.broker     = broker
        self._cache:    Optional[Tuple[str, float, float]] = None
        self._cache_ts: float = 0.0

    def detect(self) -> Dict:
        """
        Returns regime dict:
          regime        : str
          size_mult     : float  (multiply all position sizes by this)
          edge_boost    : float  (add to MIN_EDGE — positive = harder threshold)
          vix_level     : float
          allow_entries : bool
          is_bull / is_bear / is_high_fear / is_extreme : bool
        """
        now = time.monotonic()
        if self._cache and now - self._cache_ts < Config.REGIME_CACHE_SECS:
            regime, size_mult, vix = self._cache
            return self._build_result(regime, size_mult, vix)

        vix   = self._estimate_vix()
        trend = self._spy_trend()
        regime, size_mult = self._classify(vix, trend)

        self._cache    = (regime, size_mult, vix)
        self._cache_ts = now

        log.info(
            f"[Regime] {regime.upper()} | VIX≈{vix:.1f} | "
            f"trend={trend} | size_mult={size_mult:.2f}"
        )
        return self._build_result(regime, size_mult, vix)

    # ── VIX ESTIMATION ────────────────────────────────────────────────────────
    def _estimate_vix(self) -> float:
        """
        SPY realized volatility as VIX proxy.
        Uses 10-day (responsive) blended with 21-day (stable).
        Falls back to VXX if SPY data unavailable.
        """
        # PRIMARY: index realized vol (most accurate VIX proxy)
        try:
            spy_bars = self.broker.get_bars(Config.MARKET_INDEX_SYMBOL, "1Day", limit=30)
            if spy_bars and len(spy_bars) >= 10:
                closes = np.array([b.close for b in spy_bars], dtype=float)
                rets   = np.diff(np.log(closes))

                # 10-day: responsive to recent moves
                n10    = min(10, len(rets))
                vol_10 = float(np.std(rets[-n10:]) * np.sqrt(252) * 100)

                # 21-day: smoother estimate
                if len(rets) >= 21:
                    vol_21 = float(np.std(rets[-21:]) * np.sqrt(252) * 100)
                else:
                    vol_21 = vol_10

                vix = vol_10 * 0.60 + vol_21 * 0.40
                return float(np.clip(vix, 10.0, 80.0))
        except Exception:
            pass

        # FALLBACK: a volatility index quoted in VIX units (e.g. India VIX) —
        # use the level directly (no decay factor like the old VXX proxy needed).
        try:
            vix_bars = self.broker.get_bars(Config.VIX_FALLBACK_SYMBOL, "1Day", limit=3)
            if vix_bars:
                vix_est = float(vix_bars[-1].close)
                if 5.0 <= vix_est <= 80.0:
                    return vix_est
        except Exception:
            pass

        return 20.0  # safe default: assume moderate vol

    # ── SPY TREND ─────────────────────────────────────────────────────────────
    def _spy_trend(self) -> str:
        """Returns 'bull', 'bear', or 'range' using 50/200 MA cross + slope."""
        try:
            bars = self.broker.get_bars(Config.MARKET_INDEX_SYMBOL, "1Day", limit=220)
            if not bars or len(bars) < 60:
                return "range"

            closes       = np.array([b.close for b in bars], dtype=float)
            price        = float(closes[-1])
            ma50         = float(np.mean(closes[-50:]))
            ma200        = float(np.mean(closes[-200:])) if len(closes) >= 200 else float(np.mean(closes))
            slope_20d    = (closes[-1] - closes[-20]) / closes[-20] if len(closes) >= 20 else 0.0

            above_50     = price > ma50
            golden_cross = ma50 > ma200

            if above_50 and golden_cross and slope_20d > -0.01:
                return "bull"
            elif not above_50 and not golden_cross and slope_20d < 0.01:
                return "bear"
            else:
                return "range"
        except Exception:
            return "range"

    # ── CLASSIFY ──────────────────────────────────────────────────────────────
    def _classify(self, vix: float, trend: str) -> Tuple[str, float]:
        """Map (vix, trend) → (regime_name, size_multiplier)."""

        # TRUE HALT: VIX > 45 (2020/2022 crash level — circuit breaker territory)
        if vix >= Config.REGIME_VIX_EXTREME:
            return "extreme_fear", 0.0

        # HIGH FEAR: VIX 35-45 → tiny positions still allowed
        if vix >= Config.REGIME_VIX_FEAR:
            return "high_fear", 0.10

        # NORMAL REGIMES
        if trend == "bull":
            regime = "bull_low_vol" if vix < Config.REGIME_VIX_LOW else "bull_high_vol"
        elif trend == "bear":
            regime = "bear_low_vol" if vix < Config.REGIME_VIX_HIGH else "bear_high_vol"
        else:  # range
            regime = "range_low_vol" if vix < Config.REGIME_VIX_LOW else "range_high_vol"

        size_mult = Config.REGIME_SIZE_MULT.get(regime, 0.75)
        return regime, size_mult

    # ── BUILD RESULT ──────────────────────────────────────────────────────────
    def _build_result(self, regime: str, size_mult: float, vix: float) -> Dict:
        allow = regime != "extreme_fear"

        edge_adjustment = {
            "bull_low_vol":   -0.003,   # slightly easier threshold in calm bull
            "bull_high_vol":  +0.002,
            "bear_low_vol":   +0.005,
            "bear_high_vol":  +0.010,
            "range_low_vol":  +0.000,
            "range_high_vol": +0.008,
            "high_fear":      +0.020,   # require 2% extra edge in fear
            "extreme_fear":   +0.999,   # effectively blocks everything
        }.get(regime, 0.0)

        return {
            "regime":        regime,
            "size_mult":     size_mult,
            "edge_boost":    edge_adjustment,
            "vix_level":     vix,
            "allow_entries": allow,
            "is_bull":       regime.startswith("bull"),
            "is_bear":       regime.startswith("bear"),
            "is_high_fear":  regime == "high_fear",
            "is_extreme":    not allow,
        }
