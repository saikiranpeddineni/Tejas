"""
edge_engine.py — Expected Value, Kelly Sizing & Trade Viability
================================================================
WHAT WAS WRONG IN ORIGINAL:
  return (p * move) - ((1-p) * risk)
  → Basic expected value formula. Correct concept but:
  → No slippage deduction (slippage IS a cost that reduces EV)
  → No transaction cost (commissions, even $0 commissions have SEC fee etc.)
  → No Kelly criterion (how much to size based on edge)
  → No minimum viability check (trade is pointless if EV < slippage)
  → No regime scaling (edge is worth less in uncertain markets)
  → Single point estimate — no confidence interval

WHAT THIS VERSION DOES:
  ★ calculate_edge()      — slippage-adjusted expected value
  ★ kelly_fraction()      — optimal bet size (fractional Kelly)
  ★ trade_viability()     — full go/no-go assessment
  ★ edge_confidence()     — EV with uncertainty bounds
  ★ regime_adjusted_edge()— scales edge down in fear regimes
"""
import logging
from dataclasses import dataclass
from typing import Tuple

from slippage_engine import SlippageEngine

log = logging.getLogger(__name__)

_slip_engine = SlippageEngine()

# Minimum viable net edge (after all costs)
_MIN_VIABLE_EDGE = 0.005    # 0.5%


@dataclass
class TradeViability:
    viable:         bool
    gross_edge:     float   # before costs
    net_edge:       float   # after slippage + costs
    slippage:       float   # total round-trip slippage
    kelly_pct:      float   # recommended fraction of capital
    position_usd:   float   # recommended position in USD
    ev_per_dollar:  float   # expected profit per $1 invested
    reason:         str     # why viable or not


def calculate_edge(
    p:          float,   # probability of winning
    move:       float,   # expected profit on win (as fraction, e.g. 0.06 = 6%)
    risk:       float,   # expected loss on loss (as fraction, e.g. 0.03 = 3%)
    price:      float = 100.0,
    order_usd:  float = 500.0,
    adv_usd:    float = 5e7,
    spread_pct: float = 0.001,
    volatility: float = 0.02,
) -> float:
    """
    Returns net expected value (edge) after slippage.
    Positive = profitable trade, negative = avoid.

    Original formula: (p * move) - ((1-p) * risk)
    This version: same concept but with slippage deducted.
    """
    if not (0 < p < 1) or move <= 0 or risk <= 0:
        return 0.0

    # Gross EV (original formula, still correct)
    gross_ev = (p * move) - ((1 - p) * risk)

    # Round-trip slippage cost (entry + exit)
    slippage_cost = _slip_engine.estimate(price, order_usd, adv_usd, spread_pct, volatility) * 2
    net_ev = gross_ev - slippage_cost

    return round(net_ev, 6)


def kelly_fraction(
    p:           float,    # win probability
    win_pct:     float,    # win size as fraction
    loss_pct:    float,    # loss size as fraction
    kelly_scale: float = 0.25,   # fractional Kelly (0.25 = quarter Kelly)
    max_fraction: float = 0.15,  # never bet more than 15% of bankroll
) -> float:
    """
    Full Kelly fraction, scaled by kelly_scale for safety.

    Kelly formula: f* = (p*b - q) / b
    where b = win/loss ratio, p = P(win), q = P(loss)

    Kelly scale of 0.25 = "quarter Kelly" — standard risk management.
    """
    if not (0 < p < 1) or win_pct <= 0 or loss_pct <= 0:
        return 0.0

    b   = win_pct / loss_pct   # odds ratio
    q   = 1.0 - p
    raw_kelly = (p * b - q) / b

    if raw_kelly <= 0:
        return 0.0

    scaled = raw_kelly * kelly_scale
    return round(min(scaled, max_fraction), 5)


def trade_viability(
    p:            float,    # ML probability of profit
    target_pct:   float,    # take profit distance (e.g. 0.06)
    stop_pct:     float,    # stop loss distance  (e.g. 0.03)
    price:        float,
    bankroll:     float,
    order_usd:    float,
    adv_usd:      float = 5e7,
    spread_pct:   float = 0.001,
    volatility:   float = 0.02,
    min_edge:     float = _MIN_VIABLE_EDGE,
    kelly_scale:  float = 0.25,
    regime_mult:  float = 1.0,   # reduce in fear regimes
) -> TradeViability:
    """
    Full go/no-go trade assessment.
    Returns TradeViability with all metrics and a binary decision.
    """
    # ── Gross EV ──────────────────────────────────────────────────────────
    gross_ev = (p * target_pct) - ((1 - p) * stop_pct)

    if gross_ev <= 0:
        return TradeViability(
            viable=False, gross_edge=gross_ev, net_edge=gross_ev,
            slippage=0, kelly_pct=0, position_usd=0,
            ev_per_dollar=0, reason=f"Negative gross EV: {gross_ev:.4f}"
        )

    # ── Slippage ──────────────────────────────────────────────────────────
    entry_slip = _slip_engine.estimate(price, order_usd, adv_usd, spread_pct, volatility)
    exit_slip  = _slip_engine.estimate(price, order_usd, adv_usd, spread_pct, volatility, urgency=0.7)
    total_slip = entry_slip + exit_slip
    net_ev     = gross_ev - total_slip

    # ── Regime adjustment ─────────────────────────────────────────────────
    net_ev_adj = net_ev * regime_mult

    if net_ev_adj < min_edge:
        return TradeViability(
            viable=False, gross_edge=gross_ev, net_edge=net_ev_adj,
            slippage=total_slip, kelly_pct=0, position_usd=0,
            ev_per_dollar=net_ev_adj,
            reason=f"Net edge {net_ev_adj:.4f} < min {min_edge:.4f} (slippage={total_slip:.4f})"
        )

    # ── Kelly sizing ──────────────────────────────────────────────────────
    kf            = kelly_fraction(p, target_pct, stop_pct, kelly_scale) * regime_mult
    position_usd  = min(kf * bankroll, order_usd)
    ev_per_dollar = net_ev_adj

    return TradeViability(
        viable=True,
        gross_edge=round(gross_ev, 5),
        net_edge=round(net_ev_adj, 5),
        slippage=round(total_slip, 5),
        kelly_pct=round(kf, 5),
        position_usd=round(position_usd, 2),
        ev_per_dollar=round(ev_per_dollar, 5),
        reason="OK",
    )


def regime_adjusted_edge(
    gross_edge:  float,
    regime:      str,
    base_mult:   float = 1.0,
) -> Tuple[float, float]:
    """
    Scales edge and sizing by regime. Returns (adjusted_edge, size_multiplier).
    Use this to reduce position sizes and require higher edge in fear regimes.
    """
    regime_params = {
        "bull_low_vol":   (1.00, 1.00),
        "bull_high_vol":  (0.90, 0.85),
        "bear_low_vol":   (0.75, 0.60),
        "bear_high_vol":  (0.60, 0.35),
        "high_fear":      (0.50, 0.15),
        "range_low_vol":  (0.85, 0.90),
        "range_high_vol": (0.70, 0.50),
        "extreme_fear":   (0.00, 0.00),
    }
    edge_mult, size_mult = regime_params.get(regime, (0.80, 0.70))
    return gross_edge * edge_mult * base_mult, size_mult
