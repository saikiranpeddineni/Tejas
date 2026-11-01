"""
dynamic_risk.py — ★ UPGRADED Risk Manager
==========================================
New capabilities vs previous RiskManager:
  ★ VIX-adaptive position sizing (3 tiers)
  ★ Portfolio beta cap (don't exceed 1.3x market beta)
  ★ Regime-aware approvals (block longs in bear_high_vol regime)
  ★ Dynamic ATR-based stop/target calculation
  ★ Correlated position detection (prevents doubling up)
  ★ Max loss per day circuit breaker

PORTFOLIO BETA explained:
  If all your stocks move like SPY × 1.5, a -3% SPY day = -4.5% for you.
  By capping portfolio beta, you ensure the bot can't accidentally
  build a supercharged market exposure beyond your risk tolerance.
"""
import logging
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional

from brokers.base import BaseBroker
from config import Config
from models import Portfolio, Signal, Trade

log = logging.getLogger(__name__)

_SECTOR_NAMES = {0:"technology",1:"finance",2:"healthcare",
                 3:"consumer",4:"energy",5:"etf",6:"other"}

# Approximate stock betas vs NIFTY 50. Anything unmapped defaults to 1.0 —
# the old table held US tickers (NVDA/TSLA/SPY…), so every NSE stock silently
# got the default anyway while implying real coverage. A handful of high-beta
# NSE names are seeded; refine over time from live regression if needed.
_APPROX_BETA = {
    "TATAMOTORS":1.6, "TATASTEEL":1.5, "JSWSTEEL":1.4, "HINDALCO":1.5,
    "VEDL":1.7, "ADANIENT":1.6, "ADANIPORTS":1.5, "ZOMATO":1.8,
    "PAYTM":1.9, "NYKAA":1.6, "IRCTC":1.3, "YESBANK":1.7,
    "HDFCBANK":1.0, "ICICIBANK":1.05, "SBIN":1.2, "INFY":0.9,
    "TCS":0.8, "ITC":0.7, "HINDUNILVR":0.6, "NTPC":0.9, "COALINDIA":1.1,
}


class DynamicRiskManager:
    # =============================================================================
    # [MODIFIED 2026-11-01]: Add adaptive feedback engine to DynamicRiskManager
    # ORIGINAL INTENT: DynamicRiskManager received only broker instance.
    # REASON FOR CHANGE: Enable dynamic severity-scaled cooldowns, sector cluster
    # throttling, and loss velocity circuit breakers.
    # SAFE ROLLBACK: The original code is preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # def __init__(self, broker: BaseBroker):
    #     self.broker = broker
    #     self._daily_pnl = 0.0         # track P&L within the trading day
    #     self._daily_pnl_date = None   # date of last reset
    #     self._recent_exits: Dict[str, datetime] = {}   # symbol -> last exit time (re-entry cooldown)
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    def __init__(self, broker: BaseBroker, adaptive=None):
        self.broker = broker
        self.adaptive = adaptive
        self._daily_pnl = 0.0         # track P&L within the trading day
        self._daily_pnl_date = None   # date of last reset
        self._recent_exits: Dict[str, datetime] = {}   # symbol -> last exit time (re-entry cooldown)
    # =============================================================================

    # ── ★ RESTART-PROOF STATE ────────────────────────────────────────────────
    def restore_state_from_db(self) -> None:
        """Rebuild the daily-loss breaker and re-entry cooldowns from the trades
        table. Both lived only in memory, and the supervisor restarts the bot
        twice a day — so a fresh process forgot every loss booked since its
        start: the 4%/day circuit breaker could never accumulate across
        restarts, and a just-sold name could be instantly re-bought."""
        try:
            import db as _db
            from brokers.base import IST
            import datetime as _dt
            today = self.broker._now_ist().date() if hasattr(self.broker, "_now_ist") \
                else _dt.datetime.now(IST).date()
            # Re-seed from the ledger, rather than adding to an existing seed;
            # this keeps the method idempotent if startup/recovery invokes it
            # more than once on the same trading day.
            self._daily_pnl = 0.0
            self._daily_pnl_date = today

            # Realized P&L closed TODAY (IST) — seed the daily breaker.
            rows = _db.fetchall(
                "SELECT pnl_usd, exit_time FROM trades "
                "WHERE status='closed' AND exit_time IS NOT NULL"
            )
            day_start = _dt.datetime.combine(today, _dt.time.min, tzinfo=IST)
            seeded = 0.0
            for r in rows:
                try:
                    et = _dt.datetime.fromisoformat(str(r["exit_time"]))
                    if et.tzinfo is None:
                        et = et.replace(tzinfo=_dt.timezone.utc)
                    if et.astimezone(IST) >= day_start:
                        seeded += float(r["pnl_usd"] or 0)
                except Exception:
                    continue
            self._daily_pnl += seeded

            # Re-entry cooldowns: exits in the last REENTRY_COOLDOWN_HOURS.
            now = _dt.datetime.now(_dt.timezone.utc)
            for r in _db.fetchall(
                "SELECT symbol, MAX(exit_time) AS ex FROM trades "
                "WHERE status='closed' AND exit_time IS NOT NULL GROUP BY symbol"
            ):
                try:
                    et = _dt.datetime.fromisoformat(str(r["ex"]))
                    if et.tzinfo is None:
                        et = et.replace(tzinfo=_dt.timezone.utc)
                    hrs = (now - et).total_seconds() / 3600.0
                    if 0 <= hrs < Config.REENTRY_COOLDOWN_HOURS:
                        self._recent_exits[r["symbol"]] = et
                except Exception:
                    continue

            if seeded:
                log.warning(f"[Risk] Restored ₹{seeded:,.0f} of today's realized P&L "
                            f"into the daily-loss breaker (restart-proof)")
            if self._recent_exits:
                log.info(f"[Risk] Restored {len(self._recent_exits)} re-entry cooldown(s)")
        except Exception as e:
            log.warning(f"[Risk] state restore skipped: {e}")

    # ── MAIN APPROVAL ─────────────────────────────────────────────────────────
    def approve(
        self,
        signal: Signal,
        kelly_pct: float,
        raw_size: float,
        portfolio: Portfolio,
        regime: Dict,
    ) -> Optional[float]:
        """Returns approved USD size or None to reject."""
        try:
            bankroll = float(portfolio.bankroll)
            raw_size = float(raw_size)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(bankroll) or bankroll <= 0 or not math.isfinite(raw_size) or raw_size <= 0:
            log.warning("[Risk] Invalid bankroll or candidate size — rejecting")
            return None

        # ── 1. DRAWDOWN HALT ──────────────────────────────────────
        if portfolio.drawdown >= Config.MAX_DRAWDOWN_HALT:
            log.warning(f"[Risk] DRAWDOWN HALT {portfolio.drawdown:.1%}")
            return None

        # ── 2. REGIME BLOCK ───────────────────────────────────────
        if regime.get("is_extreme"):
            log.warning("[Risk] Extreme fear — no new entries")
            return None

        # Bear+high-vol: only allow oversold mean-reversion (don't blanket-block)
        if regime.get("is_bear") and regime.get("vix_level", 20) > 30:
            bb_pos = getattr(signal.features, "bb_position", 0.5)
            rsi    = getattr(signal.features, "rsi_14", 0.5)
            if bb_pos > 0.35 or rsi > 0.45:
                log.info(f"[Risk] Bear+vol: {signal.symbol} not oversold (bb={bb_pos:.2f} rsi={rsi:.2f})")
                return None

        # ── 3. DAILY LOSS CIRCUIT BREAKER ─────────────────────────
        self._maybe_reset_daily_pnl()
        max_daily_loss = bankroll * 0.04   # max 4% loss per day
        if self._daily_pnl <= -max_daily_loss:
            log.warning(f"[Risk] Daily loss limit hit: ${self._daily_pnl:.2f}")
            return None

        # ── 4. POSITION LIMIT ─────────────────────────────────────
        if len(portfolio.open_trades) >= Config.MAX_OPEN_POSITIONS:
            log.info(f"[Risk] Max positions ({Config.MAX_OPEN_POSITIONS})")
            return None

        # ── 5. DUPLICATE SYMBOL ───────────────────────────────────
        if signal.symbol in {t.symbol for t in portfolio.open_trades}:
            return None

        # =============================================================================
        # [MODIFIED 2026-11-01]: Adaptive severity-scaled cooldown & cluster breaker
        # ORIGINAL INTENT: Used fixed REENTRY_COOLDOWN_HOURS and static sector limits.
        # REASON FOR CHANGE: If adaptive engine is available, scale cooldown by loss severity
        # and enforce sector cluster throttling + macro loss velocity breakers.
        # SAFE ROLLBACK: Preserved previous logic below.
        # -----------------------------------------------------------------------------
        # [PREVIOUS CODE - PRESERVED]:
        # # ── 5b. ★ RE-ENTRY COOLDOWN ───────────────────────────────
        # # Don't immediately re-buy a name we just exited (prevents sell→rebuy churn).
        # last_exit = self._recent_exits.get(signal.symbol)
        # if last_exit is not None:
        #     hrs = (datetime.now(timezone.utc) - last_exit).total_seconds() / 3600.0
        #     if hrs < Config.REENTRY_COOLDOWN_HOURS:
        #         log.info(f"[Risk] {signal.symbol} in re-entry cooldown ({hrs:.1f}h < {Config.REENTRY_COOLDOWN_HOURS:.0f}h)")
        #         return None
        # 
        # # ── 6. SECTOR LIMIT ───────────────────────────────────────
        # sector = _SECTOR_NAMES.get(signal.features.sector_code, "other")
        # limit  = Config.SECTOR_LIMITS.get(sector, 0.20)
        # curr   = portfolio.sector_exposure.get(sector, 0.0)
        # if (curr + raw_size) / bankroll > limit:
        #     avail = limit * bankroll - curr
        #     if avail < Config.MIN_POSITION_USD:
        #         log.warning(f"[Risk] Sector {sector} at limit")
        #         return None
        #     raw_size = avail
        # -----------------------------------------------------------------------------
        # [NEW CODE]:
        # ── 5b. ★ ADAPTIVE RE-ENTRY COOLDOWN ─────────────────────
        if getattr(self, "adaptive", None) is not None:
            in_cd, hrs_left = self.adaptive.is_symbol_in_cooldown(signal.symbol)
            if in_cd:
                log.info(f"[Risk] {signal.symbol} in adaptive loss cooldown ({hrs_left:.1f}h remaining)")
                return None
        else:
            last_exit = self._recent_exits.get(signal.symbol)
            if last_exit is not None:
                hrs = (datetime.now(timezone.utc) - last_exit).total_seconds() / 3600.0
                if hrs < Config.REENTRY_COOLDOWN_HOURS:
                    log.info(f"[Risk] {signal.symbol} in re-entry cooldown ({hrs:.1f}h < {Config.REENTRY_COOLDOWN_HOURS:.0f}h)")
                    return None

        # ── 5c. ★ MACRO LOSS VELOCITY BREAKER ────────────────────
        if getattr(self, "adaptive", None) is not None:
            cluster_mult = self.adaptive.get_cluster_risk_multiplier()
            if cluster_mult <= 0.0:
                log.warning(f"[Risk] Macro loss velocity breaker active — rejecting {signal.symbol}")
                return None
            raw_size *= cluster_mult

        # ── 6. SECTOR LIMIT & SECTOR CLUSTER THROTTLE ────────────
        sector = _SECTOR_NAMES.get(signal.features.sector_code, "other")
        if getattr(self, "adaptive", None) is not None:
            is_throttled, sec_reason, sec_mult = self.adaptive.is_sector_throttled(sector)
            if is_throttled:
                log.warning(f"[Risk] {sec_reason} — rejecting {signal.symbol}")
                return None
            raw_size *= sec_mult

        limit  = Config.SECTOR_LIMITS.get(sector, 0.20)
        curr   = portfolio.sector_exposure.get(sector, 0.0)
        if (curr + raw_size) / bankroll > limit:
            avail = limit * bankroll - curr
            if avail < Config.MIN_POSITION_USD:
                log.warning(f"[Risk] Sector {sector} at limit")
                return None
            raw_size = avail
        # =============================================================================

        # ── 7. TOTAL EXPOSURE CAP & ELITE RESERVE ─────────────────
        # Normal cap is 70%. The last 15% (up to 85%) is reserved ONLY for
        # exceptional, high-conviction setups so we never miss the best trades.
        total_exposure = float(portfolio.total_exposure)
        if not math.isfinite(total_exposure) or total_exposure < 0:
            log.warning("[Risk] Invalid portfolio exposure — rejecting")
            return None
        exposure_ratio = total_exposure / bankroll

        if exposure_ratio >= 0.85:
            log.warning("[Risk] Absolute total exposure cap (85%)")
            return None

        if exposure_ratio >= 0.70:
            if signal.analyst_score < 0.40:
                log.info(f"[Risk] Reserve tier active: rejecting {signal.symbol} (score {signal.analyst_score:.2f} < 0.40)")
                return None

        # Cap the resulting book, not only the book before this order. Without
        # this headroom clamp an 80%-deployed portfolio could add a full-sized
        # order and cross the absolute 85% exposure limit.
        exposure_headroom = bankroll * 0.85 - total_exposure
        if exposure_headroom < Config.MIN_POSITION_USD:
            log.warning("[Risk] Total exposure cap leaves no enterable headroom")
            return None
        raw_size = min(raw_size, exposure_headroom)

        # ── 8. ★ PORTFOLIO BETA CAP ───────────────────────────────
        port_beta = self._estimate_portfolio_beta(portfolio.open_trades)
        new_beta  = _APPROX_BETA.get(signal.symbol, 1.0)
        if not math.isfinite(port_beta) or not math.isfinite(new_beta) or new_beta <= 0:
            log.warning("[Risk] Invalid beta estimate — rejecting")
            return None
        # port_beta is an exposure-weighted average. Compare it with the same
        # quantity after adding the candidate; the old expression added a
        # dimensionless average beta to a beta×capital fraction.
        combined_exposure = total_exposure + raw_size
        combined_beta = ((port_beta * total_exposure) + (new_beta * raw_size)) / combined_exposure
        if combined_beta > Config.MAX_PORTFOLIO_BETA and new_beta > Config.MAX_PORTFOLIO_BETA:
            beta_headroom = Config.MAX_PORTFOLIO_BETA - port_beta
            if beta_headroom <= 0:
                log.warning(f"[Risk] Portfolio beta cap ({port_beta:.2f})")
                return None
            raw_size = min(raw_size, (beta_headroom * total_exposure) /
                           (new_beta - Config.MAX_PORTFOLIO_BETA))
            log.info(f"[Risk] Beta-capped size: ₹{raw_size:.2f} (portfolio β={port_beta:.2f})")
        elif combined_beta > Config.MAX_PORTFOLIO_BETA:
            log.warning(f"[Risk] Portfolio beta cap ({combined_beta:.2f})")
            return None

        # ── 9. AVAILABLE CAPITAL ──────────────────────────────────
        if portfolio.available_capital < raw_size:
            raw_size = portfolio.available_capital
            if raw_size < Config.MIN_POSITION_USD:
                return None

        # ── ★ DYNAMIC ADJUSTMENTS ─────────────────────────────────
        # NOTE: regime size_mult is already applied inside ml.kelly_bet(), so we do
        # NOT multiply by it again here (doing so squared it — e.g. high_fear 0.10
        # became 0.01 and collapsed sizing). Only the drawdown taper is applied.
        regime_mult = regime.get("size_mult", 1.0)   # for logging/visibility only
        dd_factor   = max(0.3, 1. - portfolio.drawdown * 4)

        size = raw_size * dd_factor
        size = min(size, Config.MAX_POSITION_USD, bankroll * Config.MAX_POSITION_PCT,
                   portfolio.available_capital)
        # ★ Reject instead of inflating: the old `max(MIN_POSITION, …)` forced a
        # capped-down size BACK UP to the minimum — e.g. after the beta cap or the
        # capital clamp shrank an order below ₹5,000 it was silently bumped up
        # again, overriding the very limit that shrank it. A signal that can no
        # longer be sized at/above the minimum is simply not enterable.
        if not math.isfinite(size) or size < Config.MIN_POSITION_USD:
            log.info(f"[Risk] ⤫ {signal.symbol}: residual size ₹{size:.0f} < min "
                     f"₹{Config.MIN_POSITION_USD:.0f} after caps — reject")
            return None

        log.info(
            f"[Risk] ✓ ₹{size:.2f} | {signal.symbol} β={new_beta:.1f} "
            f"portβ={port_beta:.2f} regime_mult={regime_mult:.2f} "
            f"dd={portfolio.drawdown:.1%}"
        )
        return round(size, 2)

    # ── PORTFOLIO BETA ESTIMATION ─────────────────────────────────────────────
    def _estimate_portfolio_beta(self, open_trades: List[Trade]) -> float:
        if not open_trades: return 0.0
        total_size = sum(t.size_usd for t in open_trades)
        if total_size <= 0: return 0.0
        weighted_beta = sum(
            _APPROX_BETA.get(t.symbol, 1.0) * t.size_usd
            for t in open_trades
        )
        return weighted_beta / total_size

    # ── DAILY P&L TRACKING ────────────────────────────────────────────────────
    def record_trade_pnl(self, pnl_usd: float) -> None:
        self._maybe_reset_daily_pnl()
        self._daily_pnl += pnl_usd

    def _maybe_reset_daily_pnl(self) -> None:
        # Reset on the IST trading-day boundary, not the machine-local date — the
        # NSE session and all trade timestamps are IST/UTC, so a non-IST host would
        # otherwise reset the 4%/day breaker mid-session.
        try:
            today = self.broker._now_ist().date()
        except Exception:
            from datetime import datetime, timezone, timedelta
            today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
        if self._daily_pnl_date != today:
            self._daily_pnl = 0.0
            self._daily_pnl_date = today

    # ── PORTFOLIO STATE UPDATES ────────────────────────────────────────────────
    def open_trade(self, trade: Trade, portfolio: Portfolio) -> None:
        portfolio.open_trades.append(trade)
        sector = _SECTOR_NAMES.get(trade.features.sector_code, "other")
        portfolio.sector_exposure[sector] = (
            portfolio.sector_exposure.get(sector, 0.) + trade.size_usd
        )

    def close_trade(self, trade: Trade, portfolio: Portfolio) -> None:
        if not any(t.trade_id == trade.trade_id for t in portfolio.open_trades):
            return
        portfolio.open_trades = [t for t in portfolio.open_trades if t.trade_id != trade.trade_id]
        # NOTE: normally do NOT `portfolio.bankroll += pnl` here — equity is
        # refreshed from the broker every cycle, which already reflects realized
        # P&L once flattened. EXCEPTION: in DRY_RUN orders never reach the broker
        # book, so its equity is a constant and realized P&L would vanish — the
        # drawdown halt/taper would be permanently dead. Track it internally.
        if Config.DRY_RUN:
            portfolio.bankroll += trade.pnl_usd
            portfolio.update_peak()
        self.record_trade_pnl(trade.pnl_usd)
        self._recent_exits[trade.symbol] = datetime.now(timezone.utc)   # start re-entry cooldown
        sector = _SECTOR_NAMES.get(trade.features.sector_code, "other")
        portfolio.sector_exposure[sector] = max(
            0., portfolio.sector_exposure.get(sector, 0.) - trade.size_usd
        )
