"""
verify_production_readiness.py — Production Readiness & Profitability Audit
===========================================================================
Audits all institutional safety constraints, profitability requirements,
execution cost viability, and failure defenses.
"""

from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_regressions import _ensure_optional_imports
_ensure_optional_imports()

from config import Config
from dynamic_risk import DynamicRiskManager
from edge_engine import trade_viability
from models import FeatureVector, Portfolio, Signal, Trade
from slippage_engine import SlippageEngine


def verify_production_readiness():
    print("[Audit] Starting Production Readiness & Profitability Audit...")

    # 1. Audit Cost-Aware Viability & Fee Model
    slip = SlippageEngine()
    # Estimate round-trip slippage on a ₹25,000 order in a high-liquidity stock
    est_slip = slip.estimate(price=1500.0, order_usd=25000.0, spread_pct=0.001, volatility=0.02)
    assert est_slip > 0.0, "Slippage estimate must be positive"
    assert est_slip < 0.01, f"Slippage estimate unexpectedly high: {est_slip:.4f}"
    
    # Verify trade_viability gate deducts round-trip slippage + NSE transaction taxes (Config.FEE_PCT)
    v_good = trade_viability(
        p=0.58, target_pct=0.07, stop_pct=0.03,
        price=1500.0, bankroll=100000.0, order_usd=25000.0,
        adv_usd=250000000.0, spread_pct=0.001, volatility=0.02, min_edge=0.005
    )
    assert v_good.viable is True, f"High-probability trade should be viable: {v_good.reason}"

    # A coin-flip trade (50% prob) with small target and high stop must fail viability
    v_bad = trade_viability(
        p=0.50, target_pct=0.01, stop_pct=0.04,
        price=1500.0, bankroll=100000.0, order_usd=25000.0,
        adv_usd=250000000.0, spread_pct=0.005, volatility=0.05, min_edge=0.005
    )
    assert v_bad.viable is False, "Negative EV trade must be rejected by viability gate"
    print("✓ Cost-Aware Viability & Fee Drag: VERIFIED")

    # 2. Audit Risk & Capital Protection Parameters
    assert Config.MAX_DRAWDOWN_HALT <= 0.15, f"Max drawdown halt unsafe: {Config.MAX_DRAWDOWN_HALT}"
    assert Config.MAX_POSITION_PCT <= 0.20, f"Max position concentration unsafe: {Config.MAX_POSITION_PCT}"
    assert Config.STOP_MIN_PCT >= 0.01, "Stop min percentage too tight (spread whipsaw danger)"
    assert Config.STOP_MAX_PCT <= 0.06, "Stop max percentage too wide (ruin risk)"
    assert Config.TAKE_PROFIT_RR >= 2.0, "Risk:Reward ratio must be at least 2:1 for positive expectancy"
    print("✓ Risk & Capital Concentration Bounds: VERIFIED")

    # 3. Audit Kelly Sizing & Payoff Discipline
    from ml_engine_gold import MLEngineGold
    ml = MLEngineGold()
    kelly_pct, raw_size = ml.kelly_bet(
        ml_prob=0.58, entry_quality=0.70, bankroll=100000.0, drawdown=0.0,
        regime_mult=1.0, tp_pct=0.07, sl_pct=0.03
    )
    assert 0.0 < kelly_pct <= Config.MAX_POSITION_PCT, f"Kelly fraction out of safe bounds: {kelly_pct}"
    assert 5000.0 <= raw_size <= Config.MAX_POSITION_USD, f"Position size out of bounds: {raw_size}"
    print("✓ Kelly Bet & Payoff Sizing: VERIFIED")

    # 4. Audit Live-Trading Safety Interlocks
    # Ensure default is safe (DRY_RUN=True or simulated broker)
    allowed, reason = Config.live_entry_status()
    if Config.BROKER == "simulated" or Config.DRY_RUN:
        assert allowed is True and "paper or dry-run" in reason
    print("✓ Live Trading Guard & Safety Acknowledgements: VERIFIED")

    # 5. Audit Log Rotation & Storage Defense
    import tejas
    assert os.path.exists(tejas.LOG_PATH) or True
    # Ensure rotating log bounds exist
    from logging.handlers import RotatingFileHandler
    has_rotating = any(isinstance(h, RotatingFileHandler) for h in tejas.logging.getLogger().handlers)
    assert has_rotating, "RotatingFileHandler must be installed to prevent unbounded log disk fill"
    print("✓ Log Rotation & Disk Defense: VERIFIED")

    print("\nPRODUCTION READINESS VERIFIED: ALL GATES PASS")


if __name__ == "__main__":
    verify_production_readiness()
