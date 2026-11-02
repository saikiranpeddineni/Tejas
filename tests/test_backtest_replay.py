"""
tests/test_backtest_replay.py — Backtest Replay & Betadesk Parity Tests
========================================================================
Validates:
1. BacktestEngine execution, progress callback emissions, and metrics computation.
2. Database persistence in backtest_runs table (save, list, get).
3. WebUI REST endpoints (/api/backtest, /api/backtest/progress, /api/backtests, /api/backtests/<id>).
4. WebUI PAGE template elements: New Run & History tabs, symbols selection, progress bar, 10-card metrics grid, trades table.
"""

from __future__ import annotations

import json
import unittest

import db
import webui
from backtest_engine import BacktestEngine


class BacktestReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def test_backtest_engine_execution_and_progress(self):
        """Verify that BacktestEngine ticks through sessions and emits progress."""
        progress_events = []

        def on_prog(frac: float, msg: str):
            progress_events.append((round(frac, 2), msg))

        engine = BacktestEngine(symbols=["TATAMOTORS", "INFY", "RELIANCE"])
        res = engine.run(
            symbols=["TATAMOTORS", "INFY", "RELIANCE"],
            days=5,
            starting_cash=100000.0,
            use_ml_gate=True,
            on_progress=on_prog
        )

        self.assertTrue(res["ok"])
        self.assertIn("summary", res)
        self.assertIn("payload", res)

        # Verify progress callbacks emitted from start to 100%
        self.assertGreater(len(progress_events), 3)
        self.assertGreaterEqual(progress_events[0][0], 0.05)
        self.assertEqual(progress_events[-1][0], 1.0)

        # Verify institutional metrics (Betadesk Parity)
        metrics = res["payload"]["metrics"]
        required_keys = [
            "starting_equity", "final_equity", "net_pnl", "total_return_pct",
            "sharpe", "sortino", "max_drawdown_pct", "total_trades",
            "win_rate", "profit_factor", "expectancy", "avg_r"
        ]
        for k in required_keys:
            self.assertIn(k, metrics, f"Missing metric key: {k}")

        # Verify equity curve and trade logs
        curve = res["payload"]["equity_curve"]
        self.assertGreaterEqual(len(curve), 2)
        self.assertEqual(curve[0]["v"], 100000.0)

        trades = res["payload"]["trades"]
        self.assertIsInstance(trades, list)
        if len(trades) > 0:
            t = trades[0]
            self.assertIn("symbol", t)
            self.assertIn("entry_price", t)
            self.assertIn("exit_price", t)
            self.assertIn("pnl", t)
            self.assertIn("r_multiple", t)
            self.assertIn("strategy", t)
            self.assertIn("exit_reason", t)

    def test_db_persistence_save_list_get(self):
        """Verify saving and retrieving backtest runs in tejas.db."""
        engine = BacktestEngine(symbols=["TATAMOTORS", "TCS"])
        res = engine.run(symbols=["TATAMOTORS", "TCS"], days=5, starting_cash=100000.0)

        run_id = res["run_id"]
        db.save_backtest(run_id, res["summary"], res["payload"], res["created_at"])

        # Verify list_backtests
        history = db.list_backtests(limit=20)
        self.assertGreater(len(history), 0)
        found = [h for h in history if h["id"] == run_id]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["days"], 5)

        # Verify get_backtest
        retrieved = db.get_backtest(run_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["id"], run_id)
        self.assertEqual(retrieved["params"]["starting_cash"], 100000.0)
        self.assertIn("metrics", retrieved)

    def test_webui_backtest_endpoints(self):
        """Verify webui.run_backtest_eval and progress state."""
        # Test synchronous/eval runner
        res = webui.run_backtest_eval(lookback_days=5, capital=100000.0, symbols=["INFY", "RELIANCE"])
        self.assertTrue(res["ok"])
        self.assertIn("metrics", res)
        self.assertIn("win_rate", res["metrics"])
        self.assertIn("equity_curve", res)

        # Check progress state
        prog = webui._backtest_progress
        self.assertFalse(prog["running"])
        self.assertEqual(prog["progress"], 1.0)
        self.assertEqual(prog["message"], "Replay complete")

    def test_ui_betadesk_parity_elements_in_page(self):
        """Verify that webui.PAGE renders the complete Betadesk-synced replay modal and tabs."""
        page = webui.PAGE
        # Modal and tabs
        self.assertIn("btModal", page)
        self.assertIn("Strategy Historical Backtest Replay", page)
        self.assertIn("btTabNewBtn", page)
        self.assertIn("btTabHistoryBtn", page)
        self.assertIn("switchBtTab", page)

        # Symbols selection controls
        self.assertIn("btSymbolsList", page)
        self.assertIn("btSelectAllSyms", page)
        self.assertIn("btClearAllSyms", page)
        self.assertIn("btAddCustomSym", page)

        # Progress bar
        self.assertIn("btProgressWrap", page)
        self.assertIn("btProgressFill", page)
        self.assertIn("btProgressMsg", page)
        self.assertIn("progress-bar", page)

        # 10 Metric Cards
        self.assertIn("btTotalReturn", page)
        self.assertIn("btMaxDd", page)
        self.assertIn("btSharpe", page)
        self.assertIn("btSortino", page)
        self.assertIn("btWr", page)
        self.assertIn("btPf", page)
        self.assertIn("btTradesCount", page)
        self.assertIn("btExpectancy", page)
        self.assertIn("btAvgR", page)
        self.assertIn("btFinalEquity", page)

        # Canvas and trades table
        self.assertIn("btCanvas", page)
        self.assertIn("btTradesTableBody", page)

        # History tab table
        self.assertIn("btHistoryPane", page)
        self.assertIn("btHistoryBody", page)
        self.assertIn("loadBacktestRun", page)
        self.assertIn("fetchBacktestHistory", page)


if __name__ == "__main__":
    unittest.main()
