"""
analyst_engine.py — Transparent multi-factor stock scorer.
==========================================================
The ML model (ml_engine_gold) only becomes useful once it has learned from
dozens of resolved trades. Until then — and as a permanent sanity overlay —
this engine ranks stocks the way a disciplined systematic investor does, using
interpretable factors instead of a black box.

Each factor returns a score in [-1, +1] (+ = bullish). They are combined with
weights into a composite, mapped to a win-probability, and accompanied by a
plain-English thesis so every pick is explainable.

Factors (all derived from FeatureVector, which is already computed per stock):
  • trend        — is the stock in a confirmed uptrend? (vs SMA20/EMA200, EMA cross, higher highs)
  • momentum     — multi-horizon momentum, penalised when parabolic/overbought
  • rel_strength — is it leading the index and its sector? (winners keep winning)
  • conviction   — does volume/money-flow confirm the move? (OBV, MFI, volume)
  • risk         — quality of the setup: lower volatility / not over-extended is better
  • positioning  — buying strength, not blow-off tops; pullback-in-uptrend preferred

NOTE: valuation/quality/growth (P/E, ROE, margins, debt) are NOT included — they
need a fundamentals feed (broker API or paid data), which isn't wired yet. The
`fundamental` factor is left at 0 (neutral) with a hook to add it later.
"""
from typing import Dict
import math


def _clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def _tanh(x):
    return math.tanh(x)


class AnalystEngine:
    # Base weights (sum 1.0): a classic systematic-momentum tilt.
    WEIGHTS = {
        "trend":       0.26,
        "momentum":    0.24,
        "rel_strength":0.18,
        "conviction":  0.14,
        "risk":        0.10,
        "positioning": 0.08,
        "fundamental": 0.00,   # ★ stays 0 by design: this engine scores all 500
                               #   universe names from the FeatureVector alone.
                               #   Fundamentals (fundamentals_engine.py) apply at
                               #   the scanner's FINAL-candidate stage instead
                               #   (fragile-gate + edge tilt), so a scan never
                               #   needs 500 quoteSummary calls.
    }
    # ── Regime-adaptive weights ────────────────────────────────────────────────
    # A great investor weighs evidence differently by market context:
    #   • trending (bull): ride momentum/trend/leaders.
    #   • ranging:         fade extremes — positioning, mean-reversion, conviction.
    #   • defensive (bear/fear): prize low-risk, clean setups; distrust momentum.
    REGIME_WEIGHTS = {
        "trend":     {"trend":0.30,"momentum":0.30,"rel_strength":0.20,"conviction":0.10,"risk":0.06,"positioning":0.04,"fundamental":0.0},
        "range":     {"trend":0.16,"momentum":0.12,"rel_strength":0.16,"conviction":0.20,"risk":0.16,"positioning":0.20,"fundamental":0.0},
        "defensive": {"trend":0.18,"momentum":0.10,"rel_strength":0.14,"conviction":0.12,"risk":0.30,"positioning":0.16,"fundamental":0.0},
    }
    PROB_GAIN = 0.18   # max edge contributed: prob ∈ [0.5-0.18, 0.5+0.18]

    @classmethod
    def _weights_for(cls, regime_name: str) -> dict:
        r = (regime_name or "").lower()
        if r.startswith("bull"):
            return cls.REGIME_WEIGHTS["trend"]
        if r.startswith("range"):
            return cls.REGIME_WEIGHTS["range"]
        if r.startswith("bear") or "fear" in r:
            return cls.REGIME_WEIGHTS["defensive"]
        return cls.WEIGHTS

    # ── individual factors ────────────────────────────────────────────────────
    @staticmethod
    def _trend(f) -> float:
        s = 0.0
        s += _clip(f.price_vs_sma20 / 0.10)        # above/below 20-day
        s += _clip(f.price_vs_ema200 / 0.15)       # above/below long-term trend
        s += _clip(f.ema9_vs_ema21 / 0.05)         # short-term cross
        s += (f.higher_highs - 0.5) * 2.0          # structure (0..1 → -1..1)
        return _clip(s / 4.0)

    @staticmethod
    def _momentum(f) -> float:
        blend = (f.momentum_5d * 0.5 + f.momentum_10d * 0.3 + f.momentum_20d * 0.2)
        s = _tanh(blend * 8.0)                      # scale small daily returns
        # penalise parabolic / overbought conditions (don't chase blow-offs)
        if f.rsi_14 > 0.80 or f.williams_r > 0.90:
            s -= 0.40
        if f.macd_trend < 0:                        # momentum rolling over
            s -= 0.15
        return _clip(s)

    @staticmethod
    def _rel_strength(f) -> float:
        s = _clip(f.relative_strength / 0.20)       # vs index over ~21d
        s += _clip(f.sector_strength / 0.10) * 0.5
        return _clip(s)

    @staticmethod
    def _conviction(f) -> float:
        s = 0.0
        s += _clip(f.obv_trend)                     # on-balance volume slope
        s += (f.mfi - 0.5) * 2.0                    # money-flow index
        if f.volume_ratio > 1.5:                    # today's volume surge confirms
            s += 0.3
        elif f.volume_ratio < 0.7:                  # no participation
            s -= 0.2
        return _clip(s / 2.0)

    @staticmethod
    def _risk(f) -> float:
        # higher score = lower risk / cleaner setup
        s = 0.5
        s -= _clip((f.atr_pct - 0.02) / 0.06)       # punish high volatility
        if f.bb_position > 0.95:                     # stretched to upper band
            s -= 0.4
        s -= min(0.3, abs(f.vwap_deviation) / 0.05) # far from VWAP = chasing
        return _clip(s)

    @staticmethod
    def _positioning(f) -> float:
        # prefer strength with room to run, not extended tops
        s = 0.0
        # a little below 52w high in an uptrend is ideal; right at the high is risk
        if 0.02 <= f.dist_52w_high <= 0.20:
            s += 0.5
        elif f.dist_52w_high < 0.01:
            s -= 0.3
        # healthy RSI band (not oversold-falling, not overbought)
        if 0.45 <= f.rsi_14 <= 0.68:
            s += 0.4
        return _clip(s)

    # ── public API ──────────────────────────────────────────────────────────
    def score(self, f, regime: str = None) -> Dict:
        weights = self._weights_for(regime if isinstance(regime, str)
                                    else (regime or {}).get("regime") if isinstance(regime, dict) else None)
        factors = {
            "trend":       self._trend(f),
            "momentum":    self._momentum(f),
            "rel_strength":self._rel_strength(f),
            "conviction":  self._conviction(f),
            "risk":        self._risk(f),
            "positioning": self._positioning(f),
            "fundamental": 0.0,
        }
        composite = sum(factors[k] * weights.get(k, 0.0) for k in factors)
        composite = _clip(composite)
        prob = 0.5 + self.PROB_GAIN * _tanh(composite * 1.3)

        # confidence = how strongly factors agree with the composite direction
        sign = 1.0 if composite >= 0 else -1.0
        agree = sum(1 for k, v in factors.items()
                    if weights.get(k, 0) > 0 and v * sign > 0.05)
        active = sum(1 for k in factors if weights.get(k, 0) > 0)
        conf = _clip(0.3 + 0.7 * (agree / max(1, active)), 0.0, 1.0)

        thesis, tags = self._thesis(factors, composite)
        return {
            "prob": float(prob),
            "conf": float(conf),
            "composite": float(composite),
            "factors": {k: round(v, 3) for k, v in factors.items()},
            "thesis": thesis,
            "tags": tags,
        }

    @staticmethod
    def _thesis(factors, composite) -> tuple:
        labels = {
            "trend":       ("uptrend", "downtrend"),
            "momentum":    ("strong momentum", "weak momentum"),
            "rel_strength":("leading the market", "lagging the market"),
            "conviction":  ("volume confirms", "no volume support"),
            "risk":        ("clean low-risk setup", "high-risk/volatile"),
            "positioning": ("good entry zone", "over-extended"),
        }
        ranked = sorted(
            [(k, v) for k, v in factors.items() if k in labels],
            key=lambda kv: -abs(kv[1]),
        )
        parts, tags = [], []
        for k, v in ranked[:3]:
            if abs(v) < 0.1:
                continue
            pos, neg = labels[k]
            parts.append(pos if v > 0 else neg)
            tags.append(k + ("+" if v > 0 else "-"))
        if composite > 0.45:   verdict = "STRONG BUY"
        elif composite > 0.30: verdict = "BUY-worthy"
        elif composite > 0.12: verdict = "watch"
        elif composite > 0:    verdict = "weak"
        else:                  verdict = "avoid"
        return (f"{verdict}: " + ", ".join(parts) if parts else verdict), tags
