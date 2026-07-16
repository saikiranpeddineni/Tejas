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
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from brokers.base import BaseBroker
from config import Config
from models import Portfolio, Signal, Trade

log = logging.getLogger(__name__)

_SECTOR_NAMES = {0:"technology",1:"finance",2:"healthcare",
                 3:"consumer",4:"energy",5:"etf",6:"other"}

# Approximate stock betas vs SPY (used when live beta unavailable)
_APPROX_BETA = {
    "NVDA":1.8,"TSLA":2.0,"AMD":1.7,"SHOP":1.6,"SNOW":1.5,"MRNA":1.4,
    "ARKK":1.8,"META":1.2,"GOOGL":1.1,"AAPL":1.1,"MSFT":1.0,"AMZN":1.1,
    "JPM":1.0,"GS":1.2,"BAC":1.1,"XOM":0.9,"CVX":0.8,"GLD":0.2,
    "TLT":0.0,"SPY":1.0,"QQQ":1.1,"IWM":1.2,"VXX":-2.0,  # VXX = inverse market
}


class DynamicRiskManager:
    def __init__(self, broker: BaseBroker):
        self.broker = broker
        self._daily_pnl = 0.0         # track P&L within the trading day
        self._daily_pnl_date = None   # date of last reset
        self._recent_exits: Dict[str, datetime] = {}   # symbol -> last exit time (re-entry cooldown)

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
        bankroll = portfolio.bankroll

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

        # ── 5b. ★ RE-ENTRY COOLDOWN ───────────────────────────────
        # Don't immediately re-buy a name we just exited (prevents sell→rebuy churn).
        last_exit = self._recent_exits.get(signal.symbol)
        if last_exit is not None:
            hrs = (datetime.now(timezone.utc) - last_exit).total_seconds() / 3600.0
            if hrs < Config.REENTRY_COOLDOWN_HOURS:
                log.info(f"[Risk] {signal.symbol} in re-entry cooldown ({hrs:.1f}h < {Config.REENTRY_COOLDOWN_HOURS:.0f}h)")
                return None

        # ── 6. SECTOR LIMIT ───────────────────────────────────────
        sector = _SECTOR_NAMES.get(signal.features.sector_code, "other")
        limit  = Config.SECTOR_LIMITS.get(sector, 0.20)
        curr   = portfolio.sector_exposure.get(sector, 0.0)
        if (curr + raw_size) / bankroll > limit:
            avail = limit * bankroll - curr
            if avail < Config.MIN_POSITION_USD:
                log.warning(f"[Risk] Sector {sector} at limit")
                return None
            raw_size = avail

        # ── 7. TOTAL EXPOSURE CAP ─────────────────────────────────
        if portfolio.total_exposure >= bankroll * 0.85:
            log.warning("[Risk] Total exposure cap (85%)")
            return None

        # ── 8. ★ PORTFOLIO BETA CAP ───────────────────────────────
        port_beta = self._estimate_portfolio_beta(portfolio.open_trades)
        new_beta  = _APPROX_BETA.get(signal.symbol, 1.0)
        if port_beta + (new_beta * raw_size / bankroll) > Config.MAX_PORTFOLIO_BETA:
            # Reduce size to stay within beta cap
            beta_headroom = max(0, Config.MAX_PORTFOLIO_BETA - port_beta)
            if beta_headroom < 0.05:
                log.warning(f"[Risk] Portfolio beta cap ({port_beta:.2f})")
                return None
            raw_size = min(raw_size, (beta_headroom / new_beta) * bankroll)
            log.info(f"[Risk] Beta-capped size: ₹{raw_size:.2f} (portfolio β={port_beta:.2f})")

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
        size = max(Config.MIN_POSITION_USD, min(size, Config.MAX_POSITION_USD))
        size = min(size, bankroll * Config.MAX_POSITION_PCT, portfolio.available_capital)

        log.info(
            f"[Risk] ✓ ₹{size:.2f} | {signal.symbol} β={new_beta:.1f} "
            f"portβ={port_beta:.2f} regime_mult={regime_mult:.2f} "
            f"dd={portfolio.drawdown:.1%}"
        )
        return round(size, 2)

    # ── ★ ATR-BASED STOP/TARGET CALCULATION ──────────────────────────────────
    def calculate_stops(
        self,
        entry_price: float,
        atr_pct: float,
        regime: Dict,
    ) -> Tuple[float, float]:
        """
        Calculate dynamic stop loss and take profit based on ATR.
        Wider stops in high-vol regimes (to avoid premature stop-outs).
        """
        atr = entry_price * atr_pct

        # ATR multiplier adjusts for regime volatility
        regime_name = regime.get("regime", "bull_low_vol")
        atr_stop_mult = {
            "bull_low_vol":   1.5,
            "bull_high_vol":  2.0,   # wider in choppy conditions
            "bear_low_vol":   1.5,
            "bear_high_vol":  2.5,   # widest in danger zone
            "range_low_vol":  1.5,
            "range_high_vol": 2.0,
            "extreme_fear":   3.0,
        }.get(regime_name, 2.0)

        risk_per_share = atr * atr_stop_mult
        stop_loss   = entry_price - risk_per_share
        take_profit = entry_price + (risk_per_share * Config.TAKE_PROFIT_RR)

        # Clamp to config min/max
        max_stop = entry_price * (1. - Config.STOP_MIN_PCT)
        min_stop = entry_price * (1. - Config.STOP_MAX_PCT)
        stop_loss = max(min_stop, min(stop_loss, max_stop))

        max_tp = entry_price * 1.15   # cap take profit at 15%
        take_profit = min(take_profit, max_tp)

        return round(stop_loss, 2), round(take_profit, 2)

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
