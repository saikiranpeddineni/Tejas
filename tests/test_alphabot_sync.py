"""
tests/test_alphabot_sync.py — Test UI and Top Functionalities Sync with AlphaBot
================================================================================
Validates:
1. Complete AlphaBot REST API contracts in webui (/api/status, /api/system/heartbeat,
   /api/positions, /api/orders, /api/trades, /api/self-healing, /api/bars, /api/backtest).
2. All required fields in status() payload (state, mode, market, account, risk, system_health,
   allow_new_entries, heartbeat_diagnostics, watchlist, self_healing).
3. Position management (close single, flatten all, manual order placement).
4. Full AlphaDesk UI template rendering in PAGE (TopBar, Heartbeat, Chart, Tabs, Modals).
5. Non-regression: Trading brain and core signal/risk logic remain intact.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import webui
import db
from config import Config
from brokers.simulated import SimulatedBroker
from dynamic_risk import DynamicRiskManager
from signal_engine_gold import SignalEngineGold
from adaptive_engine import AdaptiveFeedbackEngine


class AlphaBotSyncTests(unittest.TestCase):
    # =============================================================================
    # [MODIFIED 2026-11-04]: Isolate test database from production tejas.db
    # ORIGINAL INTENT: Initialized db directly on Config.DB_PATH / webui.DB_PATH.
    # REASON FOR CHANGE: Calling webui.flatten_all_positions() during test runs
    # previously wiped live paper trading positions in tejas.db.
    # SAFE ROLLBACK: Preserved original setUpClass below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    #     @classmethod
    #     def setUpClass(cls):
    #         db.init_db()
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.test_db = os.path.join(cls.tempdir.name, "test_tejas.db")
        cls.orig_config_db = Config.DB_PATH
        cls.orig_webui_db = webui.DB_PATH
        Config.DB_PATH = cls.test_db
        webui.DB_PATH = cls.test_db
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        Config.DB_PATH = cls.orig_config_db
        webui.DB_PATH = cls.orig_webui_db
        import gc
        gc.collect()
        try:
            cls.tempdir.cleanup()
        except Exception:
            pass
    # =============================================================================

    def test_status_payload_alphabot_contract(self):
        """Verify that webui.status() satisfies both AlphaBot and legacy Tejas contracts."""
        st = webui.status()
        self.assertIsInstance(st, dict)

        # AlphaBot Contract Keys
        self.assertIn("state", st)
        self.assertIn(st["state"], ("running", "stopped", "halted"))
        self.assertIn("mode", st)
        self.assertIn(st["mode"], ("demo", "paper", "live"))
        
        self.assertIn("market", st)
        self.assertIn("is_open", st["market"])
        self.assertIn("minutes_to_close", st["market"])
        
        self.assertIn("account", st)
        self.assertIn("equity", st["account"])
        self.assertIn("day_pnl", st["account"])
        self.assertIn("day_pnl_pct", st["account"])
        self.assertIn("drawdown_pct", st["account"])

        self.assertIn("risk", st)
        self.assertIn("daily_halted", st["risk"])
        self.assertIn("max_drawdown_pct", st["risk"])

        self.assertIn("system_health", st)
        self.assertIn("allow_new_entries", st)
        self.assertIn("heartbeat_diagnostics", st)
        self.assertIn("watchlist", st)
        self.assertIn("self_healing", st)

        # Legacy Tejas Keys
        self.assertIn("running", st)
        self.assertIn("equity", st)
        self.assertIn("positions", st)
        self.assertIn("log", st)

    def test_system_heartbeat_diagnostics(self):
        """Verify heartbeat diagnostics and entry toggle logic."""
        st = webui.status()
        hb = st["heartbeat_diagnostics"]
        self.assertTrue(hb["database_connected"])
        self.assertTrue(hb["model_loaded"])

        # Toggle entries
        orig_entries = webui._allow_new_entries
        webui._allow_new_entries = False
        self.assertFalse(webui.status()["allow_new_entries"])
        webui._allow_new_entries = orig_entries

    def test_positions_and_flatten(self):
        """Verify position queries, manual ordering, and flattening."""
        # Insert a sample test position
        res = webui.execute_manual_order("TATAMOTORS", "buy", 5, "market", 950.0)
        self.assertEqual(res["status"], "filled")
        self.assertEqual(res["symbol"], "TATAMOTORS")

        # Verify it shows in orders and positions
        orders = webui.get_all_orders()
        self.assertTrue(any(o["symbol"] == "TATAMOTORS" for o in orders))

        # Close single position
        close_res = webui.close_single_position("TATAMOTORS")
        self.assertTrue(close_res["ok"])
        self.assertEqual(close_res["symbol"], "TATAMOTORS")

        # Test flatten all
        flat_res = webui.flatten_all_positions()
        self.assertTrue(flat_res["ok"])

    def test_chart_bars_and_backtest(self):
        """Verify historical bar fetching and backtest runner."""
        bars = webui.get_symbol_bars("INFY", timeframe="5Min", limit=50)
        self.assertIsInstance(bars, list)
        self.assertGreaterEqual(len(bars), 10)
        self.assertIn("o", bars[0])
        self.assertIn("c", bars[0])
        self.assertIn("h", bars[0])
        self.assertIn("l", bars[0])

        bt = webui.run_backtest_eval(lookback_days=30, capital=100000.0)
        self.assertTrue(bt["ok"])
        self.assertIn("metrics", bt)
        self.assertIn("win_rate", bt["metrics"])
        self.assertIn("profit_factor", bt["metrics"])
        self.assertIn("equity_curve", bt["metrics"])

    def test_page_alphabot_ui_elements(self):
        """Verify that webui.PAGE renders the complete AlphaDesk trading terminal."""
        page = webui.PAGE
        # Branding
        self.assertIn("Tejas Desk", page)
        self.assertIn("⚡", page)
        
        # Status & Heartbeat
        self.assertIn("heartbeat-pill", page)
        self.assertIn("marketDot", page)
        self.assertIn("hbModal", page)
        
        # Panels & Layout
        self.assertIn("watchlist-pane", page)
        self.assertIn("chart-pane", page)
        self.assertIn("mainChartCanvas", page)
        self.assertIn("tabs-deck", page)
        
        # Tabs
        self.assertIn("tabPositions", page)
        self.assertIn("tabOrders", page)
        self.assertIn("tabTrades", page)
        self.assertIn("tabSelfHealing", page)
        self.assertIn("tabLogs", page)
        
        # Modals
        self.assertIn("btModal", page)
        self.assertIn("setModal", page)
        self.assertIn("keysModal", page)
        self.assertIn("qrModal", page)
        self.assertIn("flattenModal", page)
        self.assertIn("liveModal", page)

        # Essential Working Handlers
        required_handlers = [
            "openQR", "openKeysModal", "saveKeys", "fetchBrokerFields",
            "changeMode", "handleStartStop", "openSettingsModal", "saveSettings",
            "drawChart", "drawBacktestChart", "triggerScanner", "quickAdd",
            "removeSymbol", "openFlattenModal", "confirmFlattenAll", "resetCooldowns"
        ]
        for h in required_handlers:
            self.assertIn(h, page, f"Expected handler {h} to be defined in webui.PAGE")

    def test_safe_numeric_positions_and_trades(self):
        """Verify that trade_history and open_positions guarantee non-None numbers."""
        hist = webui.trade_history()
        self.assertIsInstance(hist, list)
        for t in hist:
            self.assertIsInstance(t["entry_price"], (int, float))
            self.assertIsInstance(t["exit_price"], (int, float))
            self.assertIsInstance(t["pnl_usd"], (int, float))
            self.assertIsInstance(t["pnl_pct"], (int, float))
            self.assertIn("attribution", t)

        pos = webui.open_positions()
        self.assertIsInstance(pos, list)
        for p in pos:
            self.assertIsInstance(p["entry_price"], (int, float))
            self.assertIsInstance(p["current_price"], (int, float))
            self.assertIsInstance(p["pnl_usd"], (int, float))
            self.assertIsInstance(p["pnl_pct"], (int, float))

    def test_trading_brain_integrity(self):
        """Confirm that core ML, risk, and self-healing trading logic are untouched."""
        broker = SimulatedBroker()
        risk = DynamicRiskManager(broker=broker)
        self.assertIsNotNone(risk.broker)
        self.assertEqual(Config.MAX_DRAWDOWN_HALT, 0.10)

        adaptive = AdaptiveFeedbackEngine(broker=broker)
        self.assertEqual(adaptive.get_cluster_risk_multiplier(), 1.0)
        diag = adaptive.get_diagnostics_summary()
        self.assertIn("total_postmortems", diag)
        self.assertIn("active_cooldowns", diag)
        self.assertIn("sector_throttles", diag)


if __name__ == "__main__":
    unittest.main()
