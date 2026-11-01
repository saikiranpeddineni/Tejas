"""
Unit tests for the Self-Healing Adaptive Feedback Engine.
Deterministic, isolated, and requires no external network or broker APIs.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_regressions import _ensure_optional_imports  # noqa: E402
_ensure_optional_imports()

import db  # noqa: E402
from adaptive_engine import (  # noqa: E402
    AdaptiveFeedbackEngine,
    ATTR_STATISTICAL_NOISE,
    ATTR_REGIME_MISMATCH,
    ATTR_SECTOR_BREAKDOWN,
    ATTR_TOXIC_FEATURE,
    ATTR_EXECUTION_DRAG,
)
from config import Config  # noqa: E402
from models import FeatureVector, Trade  # noqa: E402

UTC = timezone.utc


class AdaptiveEngineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "test_tejas.db")
        self.orig_db_path = Config.DB_PATH
        Config.DB_PATH = self.db_path
        
        # Reset thread-local DB connection
        if hasattr(db._local, "conn") and db._local.conn is not None:
            try:
                db._local.conn.close()
            except Exception:
                pass
            db._local.conn = None

        db.init_db()
        self.adaptive = AdaptiveFeedbackEngine()

    def tearDown(self):
        if hasattr(db._local, "conn") and db._local.conn is not None:
            try:
                db._local.conn.close()
            except Exception:
                pass
            db._local.conn = None

        Config.DB_PATH = self.orig_db_path
        try:
            self.tempdir.cleanup()
        except Exception:
            pass

    def _make_trade(self, symbol="INFY", pnl_pct=-0.02, entry_price=1000.0, stop_loss=970.0, sector_code=0):
        now = datetime.now(UTC)
        feats = FeatureVector(
            trade_id="t-1",
            symbol=symbol,
            timestamp=now - timedelta(hours=2),
            sector_code=sector_code,
            rsi_14=0.50,
            volume_ratio=1.0,
            atr_pct=0.02,
            market_trend_20d=0.01,
        )
        return Trade(
            trade_id=f"trade-{symbol}-{datetime.now().timestamp()}",
            symbol=symbol,
            side="BUY",
            shares=10.0,
            size_usd=10000.0,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=1070.0,
            ml_probability=0.55,
            entry_quality=0.50,
            kelly_pct=0.02,
            features=feats,
            entry_time=now - timedelta(hours=2),
            pnl_usd=10000.0 * pnl_pct,
            pnl_pct=pnl_pct,
            status="open",
        )

    def test_postmortem_recording_and_persistence(self):
        trade = self._make_trade("TCS", pnl_pct=-0.02)
        pm = self.adaptive.record_postmortem(trade, exit_price=980.0, pnl_pct=-0.02, exit_reason="STOP_LOSS")
        
        self.assertEqual(pm.symbol, "TCS")
        self.assertEqual(pm.pnl_pct, -0.02)
        self.assertEqual(pm.attribution, ATTR_STATISTICAL_NOISE)

        # Verify restoration from fresh engine
        fresh_engine = AdaptiveFeedbackEngine()
        self.assertEqual(len(fresh_engine._recent_losses), 1)
        self.assertEqual(fresh_engine._recent_losses[0].symbol, "TCS")

    def test_loss_severity_scaled_cooldown(self):
        # 1. Scratch loss: 12h
        t_scratch = self._make_trade("RELIANCE", pnl_pct=-0.005)
        self.adaptive.record_postmortem(t_scratch, 995.0, -0.005, "STOP_LOSS")
        hrs = self.adaptive.get_symbol_cooldown_hours("RELIANCE")
        self.assertEqual(hrs, 12.0)

        # 2. Standard loss: 24h
        t_std = self._make_trade("HDFCBANK", pnl_pct=-0.020)
        self.adaptive.record_postmortem(t_std, 980.0, -0.020, "STOP_LOSS")
        hrs_std = self.adaptive.get_symbol_cooldown_hours("HDFCBANK")
        self.assertEqual(hrs_std, 24.0)

        # 3. Severe gap down: 48h
        t_sev = self._make_trade("TATAMOTORS", pnl_pct=-0.035)
        self.adaptive.record_postmortem(t_sev, 965.0, -0.035, "STOP_LOSS")
        hrs_sev = self.adaptive.get_symbol_cooldown_hours("TATAMOTORS")
        self.assertEqual(hrs_sev, 48.0)

        # 4. Repeat offender multiplier
        t_rep = self._make_trade("TATAMOTORS", pnl_pct=-0.025)
        self.adaptive.record_postmortem(t_rep, 975.0, -0.025, "STOP_LOSS")
        hrs_rep = self.adaptive.get_symbol_cooldown_hours("TATAMOTORS")
        # 2 losses within 14 days: 24h base * 1.5x repeat mult = 36h (or 48h * 1.5 = 72h)
        self.assertGreater(hrs_rep, 24.0)

    def test_sector_cluster_loss_breaker(self):
        # Technology sector (code 0)
        t1 = self._make_trade("INFY", pnl_pct=-0.02, sector_code=0)
        t2 = self._make_trade("WIPRO", pnl_pct=-0.02, sector_code=0)
        t3 = self._make_trade("TCS", pnl_pct=-0.02, sector_code=0)

        # 1 loss: healthy
        self.adaptive.record_postmortem(t1, 980.0, -0.02, "STOP_LOSS")
        blocked, reason, mult = self.adaptive.is_sector_throttled("technology")
        self.assertFalse(blocked)
        self.assertEqual(mult, 1.0)

        # 2 losses: throttled to 50%
        self.adaptive.record_postmortem(t2, 980.0, -0.02, "STOP_LOSS")
        blocked, reason, mult = self.adaptive.is_sector_throttled("technology")
        self.assertFalse(blocked)
        self.assertEqual(mult, 0.50)

        # 3 losses: hard blocked
        self.adaptive.record_postmortem(t3, 980.0, -0.02, "STOP_LOSS")
        blocked, reason, mult = self.adaptive.is_sector_throttled("technology")
        self.assertTrue(blocked)
        self.assertEqual(mult, 0.0)

    def test_macro_loss_velocity_breaker(self):
        # 4 stop-outs in quick succession across different symbols
        syms = ["A", "B", "C", "D"]
        for s in syms[:2]:
            t = self._make_trade(s, -0.02)
            self.adaptive.record_postmortem(t, 980.0, -0.02, "STOP_LOSS")
        self.assertEqual(self.adaptive.get_cluster_risk_multiplier(), 0.75)

        t3 = self._make_trade("C", -0.02)
        self.adaptive.record_postmortem(t3, 980.0, -0.02, "STOP_LOSS")
        self.assertEqual(self.adaptive.get_cluster_risk_multiplier(), 0.50)

        t4 = self._make_trade("D", -0.02)
        self.adaptive.record_postmortem(t4, 980.0, -0.02, "STOP_LOSS")
        self.assertEqual(self.adaptive.get_cluster_risk_multiplier(), 0.0)

    def test_toxic_feature_penalty(self):
        # Record 2 losses with overbought + thin volume
        t1 = self._make_trade("KOTAKBANK", -0.025)
        t1.features.rsi_14 = 0.72
        t1.features.volume_ratio = 0.6
        self.adaptive.record_postmortem(t1, 975.0, -0.025, "STOP_LOSS")

        t2 = self._make_trade("AXISBANK", -0.025)
        t2.features.rsi_14 = 0.70
        t2.features.volume_ratio = 0.5
        self.adaptive.record_postmortem(t2, 975.0, -0.025, "STOP_LOSS")

        # Now evaluate a new candidate with that exact same toxic profile
        candidate_feats = FeatureVector(
            trade_id="cand-1",
            symbol="SBIN",
            timestamp=datetime.now(UTC),
            rsi_14=0.71,
            volume_ratio=0.65,
        )
        penalty, reason = self.adaptive.evaluate_candidate_penalty("SBIN", candidate_feats)
        self.assertGreater(penalty, 0.0)
        self.assertIn("overbought_thin_volume", reason)


if __name__ == "__main__":
    unittest.main()
