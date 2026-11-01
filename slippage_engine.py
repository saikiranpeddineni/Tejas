"""
slippage_engine.py — Production Slippage Model
================================================
WHAT WAS WRONG IN ORIGINAL:
  return price * vol * 0.1
  → Completely wrong formula. This gives slippage as 10% of (price × volatility),
    which has no mathematical basis and produces nonsense values.
  → Ignores order size (market impact scales with size)
  → Ignores bid-ask spread (the primary source of slippage for retail)
  → No distinction between buy vs sell side

WHAT THIS VERSION DOES:
  1. Spread cost     — half the bid-ask spread, always paid
  2. Market impact   — sqrt(order_size/adv) model (Almgren-Chriss simplified)
  3. Volatility drag — high vol = more price movement during order execution
  4. Urgency factor  — market orders vs limit orders
  5. Total slippage  — all three combined, returned as a fraction

USAGE:
  from slippage_engine import SlippageEngine
  engine = SlippageEngine()
  cost = engine.estimate(price=150.0, order_usd=500.0, adv_usd=50_000_000,
                         spread_pct=0.001, volatility=0.02)
  # Returns fraction: 0.0015 means 0.15% total cost
"""
import math
import logging
from typing import Optional

log = logging.getLogger(__name__)


class SlippageEngine:

    # ── MAIN ESTIMATE ─────────────────────────────────────────────────────────
    def estimate(
        self,
        price:      float,          # current stock price
        order_usd:  float,          # size of order in USD
        adv_usd:    float = 5e7,    # average daily volume in USD (default $50M)
        spread_pct: float = 0.001,  # bid-ask spread as fraction (default 0.1%)
        volatility: float = 0.02,   # daily volatility (std of returns, default 2%)
        urgency:    float = 1.0,    # 1.0=market order, 0.5=limit, 0.0=VWAP
    ) -> float:
        """
        Returns total expected slippage as a fraction of price.
        e.g. 0.0015 = 0.15% slippage
        """
        try:
            price = float(price); order_usd = float(order_usd)
            adv_usd = float(adv_usd); spread_pct = float(spread_pct)
            volatility = float(volatility); urgency = float(urgency)
        except (TypeError, ValueError):
            return 0.0
        if not all(math.isfinite(v) for v in
                   (price, order_usd, adv_usd, spread_pct, volatility, urgency)):
            return 0.0
        if price <= 0 or order_usd <= 0:
            return 0.0

        adv_usd = max(adv_usd, 1.0)
        spread_pct = max(0.0, spread_pct)
        volatility = max(0.0, volatility)
        urgency = min(1.0, max(0.0, urgency))

        # ── Component 1: Spread Cost ───────────────────────────────────────
        # Always pay half the spread (crossing bid-ask)
        spread_cost = spread_pct / 2.0

        # ── Component 2: Market Impact ────────────────────────────────────
        # Based on simplified Almgren-Chriss permanent impact model
        # Impact = η × σ × √(Q / V)
        # where: η=0.1 (impact coefficient), σ=volatility, Q=order size, V=ADV
        participation = order_usd / max(adv_usd, 1.0)
        impact_coeff  = 0.1   # empirically estimated for US equities
        market_impact = impact_coeff * volatility * math.sqrt(participation)

        # ── Component 3: Volatility Drag ─────────────────────────────────
        # Price can move during execution. Proportional to vol × urgency.
        # For a market order on a $500 position in a 2% vol stock: ~0.02%
        execution_time = urgency * 0.01   # fraction of day (market order ≈ 1% of day)
        vol_drag       = volatility * math.sqrt(execution_time) * 0.5

        # ── Total ──────────────────────────────────────────────────────────
        total = spread_cost + (market_impact * urgency) + vol_drag
        total = max(0.0001, min(total, 0.05))   # clamp: 0.01% to 5%

        log.debug(
            f"[Slippage] ${order_usd:.0f} | spread={spread_cost:.4f} "
            f"impact={market_impact:.4f} drag={vol_drag:.4f} → total={total:.4f}"
        )
        return round(total, 6)
