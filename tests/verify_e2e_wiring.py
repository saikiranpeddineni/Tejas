"""
verify_e2e_wiring.py — End-to-End System Wiring & Integration Verification
===========================================================================
Tests full component graph instantiation, wiring, cross-module communication,
and the complete closed-loop lifecycle from signal generation to execution,
exit, post-mortem attribution, and adaptive risk feedback.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_regressions import _ensure_optional_imports
_ensure_optional_imports()

import db
from adaptive_engine import (
    AdaptiveFeedbackEngine,
    ATTR_STATISTICAL_NOISE,
    ATTR_SECTOR_BREAKDOWN,
    ATTR_TOXIC_FEATURE,
)
from brokers.simulated import SimulatedBroker
from config import Config
from dynamic_risk import DynamicRiskManager
from earnings_filter import EarningsFilter
from execution_engine import ExecutionEngine
from exit_engine import ExitEngine
from feature_builder_gold import FeatureBuilderGold
from ml_engine_gold import MLEngineGold
from models import FeatureVector, Portfolio, Signal, Trade
from regime_engine import RegimeEngine
from sentiment_engine import SentimentEngine
from signal_engine_gold import SignalEngineGold

UTC = timezone.utc


def test_wiring():
    tempdir = tempfile.TemporaryDirectory()
    test_db = os.path.join(tempdir.name, "e2e_test.db")
    old_db = Config.DB_PATH
    old_dry = Config.DRY_RUN
    Config.DB_PATH = test_db
    Config.DRY_RUN = False  # Enable simulated paper fills for test

    try:
        # 1. Reset DB
        if hasattr(db._local, "conn") and db._local.conn is not None:
            try:
                db._local.conn.close()
            except Exception:
                pass
            db._local.conn = None
        db.init_db()

        # 2. Instantiate full graph as in tejas.py
        broker = SimulatedBroker()
        ml = MLEngineGold()
        builder = FeatureBuilderGold(broker)
        earnings = EarningsFilter()
        sentiment = SentimentEngine()
        regime_e = RegimeEngine(broker)
        adaptive = AdaptiveFeedbackEngine(broker=broker, ml=ml)
        risk_mgr = DynamicRiskManager(broker, adaptive=adaptive)
        exec_e = ExecutionEngine(broker, ml, adaptive=adaptive)
        exit_e = ExitEngine(broker, ml, builder, earnings=earnings)
        sig_engine = SignalEngineGold(broker, ml, builder, earnings, sentiment, adaptive=adaptive)

        # 3. Verify Wiring References
        assert risk_mgr.adaptive is adaptive, "risk_mgr.adaptive is not wired"
        assert exec_e.adaptive is adaptive, "exec_e.adaptive is not wired"
        assert sig_engine.adaptive is adaptive, "sig_engine.adaptive is not wired"

        # 4. Create initial portfolio
        portfolio = Portfolio(bankroll=100000.0, peak_bankroll=100000.0)

        # 5. Simulate a Signal
        now = datetime.now(UTC)
        fv = FeatureVector(
            trade_id="sig-test-1",
            symbol="INFY",
            timestamp=now,
            sector_code=0,
            rsi_14=0.55,
            volume_ratio=1.2,
            atr_pct=0.02,
        )
        sig = Signal(
            symbol="INFY",
            side="BUY",
            entry_price=1000.0,
            ml_prob=0.58,
            ml_conf=0.70,
            edge=0.08,
            features=fv,
            stop_loss=970.0,
            take_profit=1060.0,
            analyst_score=0.45,
            thesis="E2E test setup",
        )

        # 6. Approve through DynamicRiskManager
        regime = {"regime": "bull_low_vol", "size_mult": 1.2, "edge_boost": 0.0}
        approved_size = risk_mgr.approve(sig, kelly_pct=0.03, raw_size=10000.0, portfolio=portfolio, regime=regime)
        assert approved_size is not None and approved_size >= 5000.0, f"Approval failed: {approved_size}"

        # 7. Execute order
        trade = exec_e.execute(sig, approved_size=approved_size, kelly_pct=0.03, entry_quality=0.60)
        assert trade is not None, "Execution failed"
        risk_mgr.open_trade(trade, portfolio)
        assert len(portfolio.open_trades) == 1, "Portfolio failed to track open trade"

        # 8. Close Trade with a Stop-Loss
        closed = exec_e.close_trade(trade, exit_price=970.0, pnl_pct=-0.03, exit_reason="STOP_LOSS")
        assert closed is True, "Close trade failed"
        risk_mgr.close_trade(trade, portfolio)
        assert len(portfolio.open_trades) == 0, "Portfolio failed to untrack closed trade"

        # 9. Verify Post-Mortem in Adaptive Engine & DB
        recent_pms = adaptive._recent_losses
        assert len(recent_pms) == 1, f"Expected 1 post-mortem, got {len(recent_pms)}"
        pm = recent_pms[0]
        assert pm.symbol == "INFY", f"Post-mortem symbol mismatch: {pm.symbol}"
        assert pm.pnl_pct < 0, "Post-mortem PnL mismatch"

        # 10. Verify Adaptive Cooldown is Active
        in_cd, hrs_left = adaptive.is_symbol_in_cooldown("INFY")
        assert in_cd is True, "INFY should be in adaptive cooldown"
        assert hrs_left > 0, "Cooldown hours remaining should be positive"

        # RiskManager should now REJECT INFY due to cooldown
        rej_size = risk_mgr.approve(sig, kelly_pct=0.03, raw_size=10000.0, portfolio=portfolio, regime=regime)
        assert rej_size is None, "Risk manager should reject symbol in adaptive cooldown"

        # 11. Test Sector Throttling Trigger
        # Add 2 more losses in IT sector (code 0)
        for s in ["TCS", "WIPRO"]:
            t = Trade(
                trade_id=f"t-{s}",
                symbol=s,
                side="BUY",
                shares=10.0,
                size_usd=10000.0,
                entry_price=1000.0,
                stop_loss=970.0,
                take_profit=1060.0,
                ml_probability=0.55,
                entry_quality=0.50,
                kelly_pct=0.02,
                features=FeatureVector(trade_id=f"f-{s}", symbol=s, timestamp=now, sector_code=0),
                entry_time=now - timedelta(hours=1),
                pnl_usd=-300.0,
                pnl_pct=-0.03,
                status="open",
            )
            adaptive.record_postmortem(t, exit_price=970.0, pnl_pct=-0.03, exit_reason="STOP_LOSS")

        blocked, reason, mult = adaptive.is_sector_throttled("technology")
        assert blocked is True, f"Technology sector should be blocked after 3 losses: {reason}"
        assert mult == 0.0, "Sector multiplier should be 0.0"

        # 12. Verify Restart Recovery
        fresh_adaptive = AdaptiveFeedbackEngine(broker=broker, ml=ml)
        assert len(fresh_adaptive._recent_losses) == 3, f"Failed to restore post-mortems: {len(fresh_adaptive._recent_losses)}"
        fresh_blocked, _, _ = fresh_adaptive.is_sector_throttled("technology")
        assert fresh_blocked is True, "Restored adaptive engine should retain sector block"

        print("E2E WIRING VERIFIED: ALL CHECKS PASSED")
    finally:
        if hasattr(db._local, "conn") and db._local.conn is not None:
            try:
                db._local.conn.close()
            except Exception:
                pass
            db._local.conn = None
        Config.DB_PATH = old_db
        Config.DRY_RUN = old_dry
        try:
            tempdir.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    test_wiring()
