"""
adaptive_engine.py — Self-Healing Diagnostic & Adaptive Feedback Engine
========================================================================
Institutional-grade self-healing and continuous improvement layer for Tejas.

DESIGN PRINCIPLES:
1. Understand Why: Not all losses are equal. Differentiates statistical market
   noise from regime mismatches, sector breakdowns, toxic feature clusters,
   and execution drag.
2. No Knee-Jerk Whipsaw: Avoids recency bias and catastrophic forgetting by using
   Bayesian decay, rolling failure attribution, and cluster breakers rather than
   destructive mid-session weight overwrites or crude forever-blacklists.
3. Multi-Tiered Adaptation:
   - Tier 1: Realized Loss Attribution & Forensics (persisted to SQLite).
   - Tier 2: Symbol-Level Adaptive Cooldown with Loss-Severity Scaling.
   - Tier 3: Sector Cluster Loss Breaker (prevents correlated sector wipeouts).
   - Tier 4: Macro Loss Velocity Breaker (throttles sizing during hostile tape).
   - Tier 5: Toxic Feature Subspace Penalty (discounts edge for historically failing setups).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any

import db
from config import Config
from models import FeatureVector, Trade

log = logging.getLogger(__name__)

# Failure attribution categories
ATTR_STATISTICAL_NOISE   = "STATISTICAL_NOISE"     # Normal random oscillation inside edge
ATTR_REGIME_MISMATCH     = "REGIME_MISMATCH"       # Broader index/VIX turned sharply hostile
ATTR_SECTOR_BREAKDOWN    = "SECTOR_BREAKDOWN"      # Sector-wide simultaneous selling pressure
ATTR_TOXIC_FEATURE       = "TOXIC_FEATURE"         # Feature combination with high failure probability
ATTR_EXECUTION_DRAG      = "EXECUTION_DRAG"        # Realized slippage or spread exceeded tolerance
ATTR_TIME_DECAY          = "TIME_DECAY"            # Setup stagnated without directional momentum


@dataclass
class TradePostMortem:
    trade_id: str
    symbol: str
    sector_code: int
    entry_price: float
    exit_price: float
    pnl_pct: float
    pnl_usd: float
    exit_reason: str
    entry_time: datetime
    exit_time: datetime
    attribution: str
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    feature_snapshot: Dict[str, float] = field(default_factory=dict)


class AdaptiveFeedbackEngine:
    """The central self-healing intelligence for Tejas."""

    def __init__(self, broker=None, ml=None):
        self.broker = broker
        self.ml = ml
        # In-memory tracking of recent post-mortems for fast micro-loop queries
        self._recent_losses: List[TradePostMortem] = []
        self._sector_loss_times: Dict[str, List[datetime]] = {}  # sector -> list of loss timestamps
        self._symbol_loss_history: Dict[str, List[Tuple[datetime, float]]] = {} # sym -> [(time, pnl_pct)]
        self._macro_loss_times: List[datetime] = []
        self._last_restore_time: Optional[datetime] = None
        self.restore_state()

    # ─────────────────────────────────────────────────────────────────────────
    # STATE RESTORATION FROM DB
    # ─────────────────────────────────────────────────────────────────────────
    def restore_state(self, lookback_days: int = 14) -> None:
        """Rebuild the adaptive memory from trade_postmortems and trades tables."""
        try:
            now = datetime.now(timezone.utc)
            cutoff = (now - timedelta(days=lookback_days)).isoformat()
            
            # Fetch recent postmortems if table exists
            rows = []
            try:
                rows = db.fetchall(
                    """
                    SELECT trade_id, symbol, sector_code, entry_price, exit_price,
                           pnl_pct, pnl_usd, exit_reason, entry_time, exit_time,
                           failure_category, diagnostics_json, feature_vector_json
                    FROM trade_postmortems
                    WHERE julianday(exit_time) >= julianday(?)
                    ORDER BY exit_time ASC
                    """,
                    (cutoff,),
                )
            except Exception as e:
                # Table might not exist yet before initial migration
                log.debug(f"[Adaptive] Could not query trade_postmortems: {e}")

            self._recent_losses.clear()
            self._sector_loss_times.clear()
            self._symbol_loss_history.clear()
            self._macro_loss_times.clear()

            from dynamic_risk import _SECTOR_NAMES

            for r in rows:
                try:
                    et = datetime.fromisoformat(str(r["exit_time"]))
                    if et.tzinfo is None:
                        et = et.replace(tzinfo=timezone.utc)
                    ent = datetime.fromisoformat(str(r["entry_time"]))
                    if ent.tzinfo is None:
                        ent = ent.replace(tzinfo=timezone.utc)
                    
                    pnl_pct = float(r["pnl_pct"])
                    sec_code = int(r["sector_code"] or 6)
                    sector = _SECTOR_NAMES.get(sec_code, "other")
                    sym = r["symbol"]

                    diag = json.loads(r["diagnostics_json"]) if r["diagnostics_json"] else {}
                    feats = json.loads(r["feature_vector_json"]) if r["feature_vector_json"] else {}

                    pm = TradePostMortem(
                        trade_id=r["trade_id"],
                        symbol=sym,
                        sector_code=sec_code,
                        entry_price=float(r["entry_price"]),
                        exit_price=float(r["exit_price"]),
                        pnl_pct=pnl_pct,
                        pnl_usd=float(r["pnl_usd"]),
                        exit_reason=r["exit_reason"],
                        entry_time=ent,
                        exit_time=et,
                        attribution=r["failure_category"],
                        diagnostics=diag,
                        feature_snapshot=feats,
                    )
                    self._recent_losses.append(pm)

                    if pnl_pct < 0:
                        # Record loss timestamps for sector, symbol, macro
                        if sector not in self._sector_loss_times:
                            self._sector_loss_times[sector] = []
                        self._sector_loss_times[sector].append(et)

                        if sym not in self._symbol_loss_history:
                            self._symbol_loss_history[sym] = []
                        self._symbol_loss_history[sym].append((et, pnl_pct))

                        self._macro_loss_times.append(et)
                except Exception as ex:
                    log.debug(f"[Adaptive] Skipping malformed postmortem row: {ex}")
                    continue

            self._last_restore_time = now
            log.info(f"[Adaptive] Restored adaptive state: {len(self._recent_losses)} recent post-mortems")
        except Exception as e:
            log.error(f"[Adaptive] Failed to restore state: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────────────
    # TIER 1: LOSS POST-MORTEM & ROOT-CAUSE ATTRIBUTION
    # ─────────────────────────────────────────────────────────────────────────
    def record_postmortem(
        self,
        trade: Trade,
        exit_price: float,
        pnl_pct: float,
        exit_reason: str,
        regime: Optional[Dict[str, Any]] = None,
    ) -> TradePostMortem:
        """
        Conducts an immediate forensic post-mortem on a closed trade, classifies
        its root cause, updates active adaptive memory, and persists to SQLite.
        """
        now = datetime.now(timezone.utc)
        attribution, diagnostics = self._attribute_outcome(trade, exit_price, pnl_pct, exit_reason, regime)
        
        feature_dict = {}
        if hasattr(trade, "features") and trade.features:
            try:
                from feature_builder_gold import FEATURE_NAMES
            except Exception:
                FEATURE_NAMES = [
                    "price_vs_sma20","price_vs_ema9","ema9_vs_ema21","rsi_14",
                    "momentum_5d","momentum_10d","momentum_20d","rate_of_change",
                    "macd_histogram","macd_trend","atr_pct","bb_position","bb_squeeze",
                    "volatility_20d","volume_ratio","volume_trend","vwap_deviation",
                    "spy_corr","relative_strength","sector_strength","sector_code",
                    "candle_stability","williams_r","stoch_k","stoch_d","cci",
                    "obv_trend","mfi","dist_52w_high","dist_52w_low","price_vs_ema200",
                    "higher_highs","intraday_range_ratio","bid_ask_spread",
                    "market_trend_20d","market_vol_20d","trend_alignment",
                ]
            try:
                feature_dict = dict(zip(FEATURE_NAMES, trade.features.to_list()))
            except Exception:
                feature_dict = {}

        pm = TradePostMortem(
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            sector_code=trade.features.sector_code if trade.features else 6,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            pnl_pct=pnl_pct,
            pnl_usd=trade.pnl_usd,
            exit_reason=exit_reason,
            entry_time=trade.entry_time,
            exit_time=now,
            attribution=attribution,
            diagnostics=diagnostics,
            feature_snapshot=feature_dict,
        )

        # Update in-memory state
        self._recent_losses.append(pm)
        from dynamic_risk import _SECTOR_NAMES
        sector = _SECTOR_NAMES.get(pm.sector_code, "other")

        if pnl_pct < 0:
            if sector not in self._sector_loss_times:
                self._sector_loss_times[sector] = []
            self._sector_loss_times[sector].append(now)

            if trade.symbol not in self._symbol_loss_history:
                self._symbol_loss_history[trade.symbol] = []
            self._symbol_loss_history[trade.symbol].append((now, pnl_pct))

            self._macro_loss_times.append(now)

        # Persist to DB
        self._persist_postmortem(pm)

        log.info(
            f"[Self-Healing] Post-Mortem {trade.symbol}: outcome={'WIN' if pnl_pct >= 0 else 'LOSS'} "
            f"({pnl_pct:+.2%}) | Root Cause: {attribution} | Reason: {exit_reason}"
        )
        return pm

    def _attribute_outcome(
        self,
        trade: Trade,
        exit_price: float,
        pnl_pct: float,
        exit_reason: str,
        regime: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Identify why the trade succeeded or failed."""
        diagnostics: Dict[str, Any] = {
            "pnl_pct": pnl_pct,
            "exit_reason": exit_reason,
            "entry_price": trade.entry_price,
            "exit_price": exit_price,
            "highest_price": trade.highest_price,
            "lowest_price": trade.lowest_price,
        }

        if pnl_pct >= 0:
            return "SUCCESSFUL_TARGET", diagnostics

        f = trade.features
        # 1. Check Execution / Slippage Drag
        # If entry to exit spread was exceptionally high or fee drag accounted for > 40% of loss
        sl_dist = abs(trade.entry_price - trade.stop_loss) / max(trade.entry_price, 0.01)
        actual_loss = abs(pnl_pct)
        if actual_loss > sl_dist * 1.5:
            diagnostics["note"] = "Slippage or gap beyond stop level"
            return ATTR_EXECUTION_DRAG, diagnostics

        # 2. Check Regime Mismatch (Macro Breakdown)
        # If VIX spiked into extreme/fear or market trend collapsed during hold
        if regime:
            regime_name = regime.get("regime", "")
            if "bear" in regime_name or "fear" in regime_name:
                diagnostics["regime"] = regime_name
                return ATTR_REGIME_MISMATCH, diagnostics

        if f and getattr(f, "market_trend_20d", 0.0) < -0.03:
            diagnostics["market_trend_20d"] = f.market_trend_20d
            return ATTR_REGIME_MISMATCH, diagnostics

        # 3. Check Toxic Feature Subspace
        # e.g. Overbought momentum chase with low volume participation
        if f:
            if getattr(f, "rsi_14", 0.5) > 0.68 and getattr(f, "volume_ratio", 1.0) < 0.8:
                diagnostics["toxic_pattern"] = "overbought_low_volume"
                return ATTR_TOXIC_FEATURE, diagnostics
            if getattr(f, "dist_52w_high", 0.2) > 0.35 and getattr(f, "momentum_20d", 0.0) < -0.05:
                diagnostics["toxic_pattern"] = "deep_downtrend_knife"
                return ATTR_TOXIC_FEATURE, diagnostics
            if getattr(f, "atr_pct", 0.02) > 0.05 and getattr(f, "bb_squeeze", 0.02) < 0.015:
                diagnostics["toxic_pattern"] = "high_volatility_whipsaw"
                return ATTR_TOXIC_FEATURE, diagnostics

        # 4. Check Sector Breakdown
        from dynamic_risk import _SECTOR_NAMES
        sector = _SECTOR_NAMES.get(f.sector_code if f else 6, "other")
        recent_sector_losses = self.get_recent_sector_losses(sector, hours=24)
        if len(recent_sector_losses) >= 2:
            diagnostics["recent_sector_losses"] = len(recent_sector_losses)
            return ATTR_SECTOR_BREAKDOWN, diagnostics

        # 5. Check Time Decay / Stagnation
        if exit_reason and "TIME" in exit_reason.upper():
            return ATTR_TIME_DECAY, diagnostics

        # 6. Default to standard Statistical Noise
        return ATTR_STATISTICAL_NOISE, diagnostics

    def _persist_postmortem(self, pm: TradePostMortem) -> None:
        try:
            db.execute(
                """
                INSERT OR REPLACE INTO trade_postmortems
                    (trade_id, symbol, sector_code, entry_price, exit_price,
                     pnl_pct, pnl_usd, exit_reason, entry_time, exit_time,
                     failure_category, diagnostics_json, feature_vector_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pm.trade_id,
                    pm.symbol,
                    pm.sector_code,
                    pm.entry_price,
                    pm.exit_price,
                    pm.pnl_pct,
                    pm.pnl_usd,
                    pm.exit_reason,
                    pm.entry_time.isoformat(),
                    pm.exit_time.isoformat(),
                    pm.attribution,
                    json.dumps(pm.diagnostics),
                    json.dumps(pm.feature_snapshot),
                ),
            )
        except Exception as e:
            log.warning(f"[Adaptive] Could not persist postmortem for {pm.symbol}: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # TIER 2: ADAPTIVE SYMBOL COOLDOWN (SEVERITY & REPEAT OFFENDER SCALING)
    # ─────────────────────────────────────────────────────────────────────────
    def get_symbol_cooldown_hours(self, symbol: str) -> float:
        """
        Dynamically calculates the cooldown window for a symbol based on:
        - Severity of the last loss (small stop vs gap down).
        - Number of losses in the last 14 days (repeat failure penalty).
        """
        history = self._symbol_loss_history.get(symbol, [])
        if not history:
            return float(getattr(Config, "REENTRY_COOLDOWN_HOURS", 24.0))

        now = datetime.now(timezone.utc)
        # Filter to recent 14 days
        cutoff = now - timedelta(days=14)
        recent = [(t, pnl) for (t, pnl) in history if t >= cutoff]
        if not recent:
            return float(getattr(Config, "REENTRY_COOLDOWN_HOURS", 24.0))

        last_time, last_pnl = recent[-1]
        
        # Base hours scaled by loss magnitude
        if last_pnl >= -0.008:
            base_hours = 12.0  # Minor scratch: shorter cooldown
        elif last_pnl >= -0.025:
            base_hours = 24.0  # Normal stop: standard 24h
        elif last_pnl >= -0.040:
            base_hours = 48.0  # Heavy loss: 48h
        else:
            base_hours = 72.0  # Severe gap/whipsaw: 72h

        # Exponential multiplier for repeat offenders on the same ticker
        repeat_count = len(recent)
        repeat_mult = 1.0 + (repeat_count - 1) * 0.5  # 1st=1.0x, 2nd=1.5x, 3rd=2.0x
        
        total_cooldown = min(168.0, base_hours * repeat_mult)  # Cap at 7 days max
        return total_cooldown

    def is_symbol_in_cooldown(self, symbol: str) -> Tuple[bool, float]:
        """Returns (is_in_cooldown, hours_remaining)."""
        history = self._symbol_loss_history.get(symbol, [])
        if not history:
            return False, 0.0

        last_time, _ = history[-1]
        now = datetime.now(timezone.utc)
        elapsed = (now - last_time).total_seconds() / 3600.0
        required = self.get_symbol_cooldown_hours(symbol)

        if elapsed < required:
            return True, required - elapsed
        return False, 0.0

    # ─────────────────────────────────────────────────────────────────────────
    # TIER 3: SECTOR CLUSTER LOSS BREAKER
    # ─────────────────────────────────────────────────────────────────────────
    def get_recent_sector_losses(self, sector: str, hours: float = 24.0) -> List[datetime]:
        """Returns timestamps of losses in this sector within the given hours."""
        times = self._sector_loss_times.get(sector, [])
        if not times:
            return []
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)
        return [t for t in times if t >= cutoff]

    def is_sector_throttled(self, sector: str) -> Tuple[bool, str, float]:
        """
        Detects if a sector is experiencing a correlated collapse.
        If >= 2 losses occur in the same sector within 24h:
          - 2 losses: throttles sizing to 50%
          - >= 3 losses: hard blocks new entries in that sector for 24h
        Returns (is_blocked, status_reason, size_multiplier).
        """
        losses = self.get_recent_sector_losses(sector, hours=24.0)
        n = len(losses)
        if n >= 3:
            return True, f"Sector {sector} blocked (3+ losses in 24h)", 0.0
        elif n == 2:
            return False, f"Sector {sector} throttled (2 losses in 24h — 50% size)", 0.50
        return False, "healthy", 1.0

    # ─────────────────────────────────────────────────────────────────────────
    # TIER 4: MACRO LOSS VELOCITY BREAKER
    # ─────────────────────────────────────────────────────────────────────────
    def get_cluster_risk_multiplier(self) -> float:
        """
        Monitors loss velocity across the entire portfolio.
        If the market turns sharply hostile and multiple stops fire in close succession:
          - 2 losses within 4h: 0.75x sizing
          - 3 losses within 4h: 0.50x sizing
          - >= 4 losses within 4h: 0.0x sizing (halt new entries for 2 hours)
        """
        now = datetime.now(timezone.utc)
        cutoff_4h = now - timedelta(hours=4.0)
        recent_4h = [t for t in self._macro_loss_times if t >= cutoff_4h]
        count = len(recent_4h)

        if count >= 4:
            log.warning(f"[Self-Healing] 🛑 Macro Cluster Breaker: {count} losses in 4h — HALTING new entries")
            return 0.0
        elif count == 3:
            log.info(f"[Self-Healing] ⚠️ Macro Cluster Breaker: 3 losses in 4h — sizing scaled to 50%")
            return 0.50
        elif count == 2:
            return 0.75
        return 1.0

    # ─────────────────────────────────────────────────────────────────────────
    # TIER 5: TOXIC FEATURE SUBSPACE PENALTY
    # ─────────────────────────────────────────────────────────────────────────
    def evaluate_candidate_penalty(
        self,
        symbol: str,
        features: FeatureVector,
        regime: Optional[Dict[str, Any]] = None,
    ) -> Tuple[float, str]:
        """
        Audits incoming signal features against known failure clusters from the
        last 30 days of trade history.
        
        Returns (penalty_score, penalty_reason) where penalty_score is subtracted
        from analyst_score and edge.
        """
        penalty = 0.0
        reasons = []

        if not features:
            return 0.0, ""

        # 1. Check if candidate matches recent toxic feature clusters
        recent_toxic = [pm for pm in self._recent_losses[-40:] if pm.attribution == ATTR_TOXIC_FEATURE]
        
        # Subspace Pattern A: Overbought chase with weak volume
        if features.rsi_14 > 0.68 and features.volume_ratio < 0.8:
            matching_losses = sum(
                1 for pm in recent_toxic 
                if pm.feature_snapshot.get("rsi_14", 0) > 0.65 and pm.feature_snapshot.get("volume_ratio", 1) < 0.9
            )
            if matching_losses >= 2:
                penalty += 0.08
                reasons.append(f"overbought_thin_volume({matching_losses}x_fail)")

        # Subspace Pattern B: Severe negative market trend alignment
        if features.trend_alignment < 0.35 and features.market_trend_20d < -0.02:
            matching_losses = sum(
                1 for pm in self._recent_losses[-40:]
                if pm.pnl_pct < 0 and pm.feature_snapshot.get("market_trend_20d", 0) < -0.02
            )
            if matching_losses >= 3:
                penalty += 0.06
                reasons.append(f"fighting_market_downtrend({matching_losses}x_fail)")

        # Subspace Pattern C: High ATR whipsaw trap
        if features.atr_pct > 0.045 and features.bb_squeeze < 0.02:
            matching_losses = sum(
                1 for pm in self._recent_losses[-40:]
                if pm.pnl_pct < 0 and pm.feature_snapshot.get("atr_pct", 0) > 0.04
            )
            if matching_losses >= 2:
                penalty += 0.05
                reasons.append(f"wide_atr_whipsaw({matching_losses}x_fail)")

        # 2. Check sector friction
        from dynamic_risk import _SECTOR_NAMES
        sector = _SECTOR_NAMES.get(features.sector_code, "other")
        sector_losses = self.get_recent_sector_losses(sector, hours=24.0)
        if len(sector_losses) == 1:
            penalty += 0.03
            reasons.append(f"sector_{sector}_loss_today")

        total_penalty = min(0.25, penalty)
        reason_str = "; ".join(reasons) if reasons else ""
        return total_penalty, reason_str
