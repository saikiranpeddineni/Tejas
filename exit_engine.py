"""
exit_engine.py — 4-Trigger Exit System for US Stocks
======================================================
TRIGGER 1: STOP_LOSS       — price hits stop loss level
TRIGGER 2: TAKE_PROFIT     — price hits take profit level
TRIGGER 3: TRAILING_STOP   — trailing stop from peak (protects profits)
TRIGGER 4: ML_REVERSAL     — ML re-score shows probability drop
TRIGGER 5: TIME_DECAY      — position held too long with no progress
TRIGGER 6: MARKET_CLOSE    — force close before end of day (optional)

⚠️  STOPS ARE SOFTWARE-ONLY. There is no native/server-side bracket for NSE
    delivery equity — `place_bracket_order` places a plain market entry and THIS
    engine is the sole enforcer of SL/TP/trailing. That means stops only fire
    while this process is alive and monitoring every open trade each cycle. If
    the process dies, open positions are unprotected on the exchange. Keep the
    watchdog/supervisor running, and never rely on an exchange-side stop.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from analyst_engine import AnalystEngine
from brokers.base import BaseBroker
from config import Config
from feature_builder_gold import FeatureBuilderGold
from ml_engine_gold import MLEngineGold
from models import Bar, Trade

log = logging.getLogger(__name__)

EMA_ALPHA = 0.25


class ExitEngine:
    def __init__(self, broker: BaseBroker, ml: MLEngineGold, feature_builder: FeatureBuilderGold):
        self.broker   = broker
        self.ml       = ml
        self.builder  = feature_builder
        self.analyst  = AnalystEngine()
        self._regime: Dict = {"regime": "unknown"}
        self._state: Dict[str, dict] = {}
        self._bars_cache: Dict[str, Tuple[List[Bar], float]] = {}

    def monitor(self, open_trades: List[Trade], regime: Dict = None) -> List[Tuple[Trade, str, float, float]]:
        """
        Check all open trades for exit conditions.
        Returns list of (trade, reason, exit_price, pnl_pct).
        """
        if regime:
            self._regime = regime
        exits = []
        for trade in open_trades:
            result = self._check(trade)
            if result:
                reason, exit_price, pnl_pct = result
                exits.append((trade, reason, exit_price, pnl_pct))
        return exits

    def _check(self, trade: Trade) -> Optional[Tuple[str, float, float]]:
        """Check single trade. Returns (reason, exit_price, pnl_pct) or None."""
        now = time.monotonic()
        state = self._get_state(trade)

        if now - state["last_check"] < Config.MONITOR_INTERVAL_SECS:
            return None
        state["last_check"] = now

        # ── ★ Exits only price against a LIVE market ──────────────
        # Off-hours the quote is Friday's close (or worse): time-decay is pure
        # wall-clock so it used to fire at 2am Saturday and book an exit at a
        # stale price. Prices don't move while the market is shut — there is
        # nothing to protect against; re-check at the next open.
        if not self.broker.is_market_open():
            return None

        # Live price — if the feed fails, SKIP this check rather than fabricate
        # a price from entry_price (which invents pnl=0 exits with real fees).
        quote = self.broker.get_quote(trade.symbol, force=True)
        if quote is None or quote.mid <= 0:
            log.debug(f"[Exit] {trade.symbol}: no quote — skipping this check")
            return None
        current_price = quote.mid
        pnl_pct = (current_price - trade.entry_price) / trade.entry_price

        # Update trailing high-water mark (persisted so restarts don't disarm it)
        if trade.side == "BUY":
            if trade.highest_price is None or current_price > trade.highest_price:
                trade.highest_price = current_price
                self._persist_hwm(trade)

        # ── TRIGGER 1: HARD STOP LOSS ─────────────────────────────
        if current_price <= trade.stop_loss:
            return "STOP_LOSS", current_price, pnl_pct

        # ── TRIGGER 2: TAKE PROFIT ────────────────────────────────
        if current_price >= trade.take_profit:
            return "TAKE_PROFIT", current_price, pnl_pct

        # ── TRIGGER 3: TRAILING STOP ──────────────────────────────
        if trade.highest_price and trade.highest_price > trade.entry_price * 1.02:
            # Only activate trailing stop after 2%+ gain
            trail_price = trade.highest_price * (1.0 - Config.TRAILING_STOP_PCT)
            if current_price < trail_price:
                return f"TRAILING_STOP_from_{trade.highest_price:.2f}", current_price, pnl_pct

        # ── TRIGGER 6: ★ PARTIAL TAKE-PROFIT (trim + ride) ────────
        # At PARTIAL_TP_AT of the TP distance, sell PARTIAL_TP_FRACTION and move
        # the stop to breakeven (the main loop handles the trim; the trade stays
        # open). Replaces the old EOD "close the whole winner near target" —
        # a trim books the same protection while keeping the remainder's upside,
        # and the breakeven stop bounds the overnight gap risk. Sits ABOVE the
        # min-hold gate deliberately: a day-one runner at the target zone is
        # exactly the gain this protects.
        if Config.USE_PARTIAL_EXITS and trade.trim_shares == 0:
            tp_dist = (trade.take_profit - trade.entry_price) / trade.entry_price
            if tp_dist > 0 and pnl_pct >= Config.PARTIAL_TP_AT * tp_dist:
                return "PARTIAL_TP", current_price, pnl_pct

        # ── ★ MINIMUM HOLD GATE ───────────────────────────────────
        # Hard SL/TP/trailing/EOD-harvest above always fire. But the soft, model-
        # driven exits (ML reversal, time decay) are suppressed until the position
        # has had time to work — the model forecasts a multi-day move, so flushing
        # it the same session on a tiny probability wobble is just churn.
        if self._age_hours(trade) < Config.MIN_HOLD_HOURS:
            return None

        # ── TRIGGER 4: ML PROBABILITY REVERSAL ───────────────────
        result = self._check_ml_reversal(trade, state, quote, pnl_pct)
        if result:
            return result, current_price, pnl_pct

        # ── TRIGGER 5: TIME DECAY ─────────────────────────────────
        result = self._check_time_decay(trade, pnl_pct)
        if result:
            return result, current_price, pnl_pct

        return None

    @staticmethod
    def _persist_hwm(trade: Trade) -> None:
        """Write the new high-water mark so a restart restores trailing state."""
        try:
            import db
            db.execute("UPDATE trades SET highest_price=? WHERE trade_id=?",
                       (trade.highest_price, trade.trade_id))
        except Exception as e:
            log.debug(f"[Exit] HWM persist failed {trade.symbol}: {e}")

    def _check_ml_reversal(self, trade: Trade, state: dict, quote, pnl_pct: float) -> Optional[str]:
        """Re-score the SAME way entry did (blended ML+analyst, live quote) and exit
        only on a *confirmed* deterioration that is also losing money.

        Why this shape: the old version compared the entry blend (ML+analyst, quote-
        aware) against a raw, quote-less re-score — an apples-to-oranges gap that
        biased the "drop" upward and flushed whole cohorts at breakeven. We now
        (1) compute the exit prob identically to entry, (2) require N consecutive
        breaches (hysteresis), and (3) require the trade to actually be in the red.
        """
        try:
            bars = self._get_bars(trade.symbol)
            if not bars or len(bars) < 30:
                return None

            # Rebuild features with the live quote, exactly like the entry scan.
            features = self.builder.build(trade.symbol, bars, quote)
            if features is None:
                return None

            # Same blend the entry used: 0.60·ML + 0.40·analyst (if a model exists).
            ml_prob, _ = self.ml.predict(features)
            view = self.analyst.score(features, self._regime)
            ml_trained = self.ml.is_trustworthy()   # same gate as entry
            curr_prob = (0.60 * ml_prob + 0.40 * view["prob"]) if ml_trained else view["prob"]

            # EMA smooth
            ema_prob = EMA_ALPHA * curr_prob + (1.0 - EMA_ALPHA) * state["ema_prob"]
            state["ema_prob"] = ema_prob
            trade.last_ml_prob = ema_prob

            entry_prob = trade.ml_probability
            if entry_prob <= 0:
                return None

            drop = (entry_prob - ema_prob) / entry_prob
            breached = (drop > Config.PROB_DROP_THRESHOLD) or (ema_prob < 0.45 and entry_prob > 0.60)

            # Only count a breach as real if the position is also losing — a name
            # that's flat or green doesn't need a probability-based bail-out.
            if breached and pnl_pct <= Config.ML_EXIT_REQUIRE_LOSS:
                state["breach_count"] += 1
                if state["breach_count"] >= Config.ML_REVERSAL_CONFIRM_N:
                    return f"ML_REVERSAL_{drop:.1%}_ema={ema_prob:.3f}_n={state['breach_count']}"
            else:
                state["breach_count"] = 0

        except Exception as e:
            log.debug(f"[Exit] ML check error {trade.symbol}: {e}")
        return None

    def _check_time_decay(self, trade: Trade, pnl_pct: float) -> Optional[str]:
        """Exit if the thesis has had ample time and isn't working. Based on wall-
        clock age since entry (persistent across restarts), not a monotonic timer
        that silently reset every time the process bounced.

        "Not working" = anything below +1%, INCLUDING losers. The old band
        (abs(pnl) < 1%) had a hole: a trade at −1.5%…−5.9% could never time-decay,
        so failed names sat for 75h+ until the hard stop or a rotation realized a
        far bigger loss (ZFCVINDIA −6.26%). A red trade past the decay window is
        the clearest "thesis failed" signal there is — cut it, don't wait for SL."""
        age_hours = self._age_hours(trade)
        if age_hours > Config.TIME_DECAY_HOURS and pnl_pct < 0.01:
            tag = "losing" if pnl_pct < -0.005 else "no_progress"
            return f"TIME_DECAY_{age_hours:.0f}h_{tag}"
        return None

    @staticmethod
    def _age_hours(trade: Trade) -> float:
        """Hours since entry, robust to naive/aware datetimes."""
        et = trade.entry_time
        if et is None:
            return 1e9
        now = datetime.now(timezone.utc)
        if et.tzinfo is None:
            et = et.replace(tzinfo=timezone.utc)
        return (now - et).total_seconds() / 3600.0

    def _get_bars(self, symbol: str) -> Optional[List[Bar]]:
        now = time.monotonic()
        cached = self._bars_cache.get(symbol)
        if cached and now - cached[1] < 300:  # 5min cache
            return cached[0]
        # Same lookback as ENTRY (220): re-scoring on 60 bars silently zeroed
        # price_vs_ema200 and shrank dist_52w to a quarter-window, making the
        # "identical to entry" exit probability structurally different from the
        # entry one and biasing spurious ML_REVERSAL exits.
        bars = self.broker.get_bars(symbol, "1Day", limit=Config.SIGNAL_LOOKBACK_BARS)
        if bars:
            self._bars_cache[symbol] = (bars, now)
        return bars

    def _get_state(self, trade: Trade) -> dict:
        if trade.trade_id not in self._state:
            self._state[trade.trade_id] = {
                "ema_prob":       trade.ml_probability,
                "last_check":     0.0,
                "breach_count":   0,
            }
        return self._state[trade.trade_id]

    def purge(self, closed_ids: List[str], symbols: Optional[List[str]] = None) -> None:
        """Drop per-trade exit state for closed trades, and optionally evict their
        bars from the cache so a re-entered name doesn't reuse stale bars."""
        for tid in closed_ids:
            self._state.pop(tid, None)
        for sym in (symbols or []):
            self._bars_cache.pop(sym, None)
