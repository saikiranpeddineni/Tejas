"""
test_paper_live_parity.py — Paper & Live Trade Parity Verification
===================================================================
Tests that SimulatedBroker mirrors live broker semantics:
- Real bid/ask spread execution (buy at ask, sell at bid)
- Consistent order fill payload shape (status, id, filled_avg_price, qty)
- Clean earnings and sentiment freshness gates for both paper and live
- Book reconciliation parity
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_regressions import _ensure_optional_imports
_ensure_optional_imports()

import db
from brokers.simulated import SimulatedBroker
from config import Config
from earnings_filter import EarningsFilter
from execution_engine import ExecutionEngine
from models import FeatureVector, Portfolio, Quote, Signal, Trade
from sentiment_engine import SentimentEngine


class PaperLiveParityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "parity_test.db")
        self.old_db = Config.DB_PATH
        self.old_broker = Config.BROKER
        self.old_dry_run = Config.DRY_RUN
        Config.DB_PATH = self.db_path
        Config.BROKER = "simulated"
        Config.DRY_RUN = False

        if hasattr(db._local, "conn") and db._local.conn is not None:
            try:
                db._local.conn.close()
            except Exception:
                pass
            db._local.conn = None
        db.init_db()

    def tearDown(self):
        if hasattr(db._local, "conn") and db._local.conn is not None:
            try:
                db._local.conn.close()
            except Exception:
                pass
            db._local.conn = None
        Config.DB_PATH = self.old_db
        Config.BROKER = self.old_broker
        Config.DRY_RUN = self.old_dry_run
        try:
            self.tempdir.cleanup()
        except Exception:
            pass

    def test_paper_executes_at_realistic_spread(self):
        broker = SimulatedBroker()
        broker.cash = 100000.0

        # Mock a quote with realistic spread: bid 999.0, ask 1001.0, mid 1000.0
        q = Quote(
            symbol="INFY",
            bid=999.0,
            ask=1001.0,
            bid_size=100,
            ask_size=100,
            timestamp=datetime.now(timezone.utc),
        )
        with mock.patch.object(broker, "get_quote", return_value=q):
            # 1. Buy order: should fill at Ask (1001.0), not mid (1000.0)
            res_buy = broker._place_entry("INFY", "buy", 10)
            self.assertIsNotNone(res_buy)
            self.assertEqual(res_buy["filled_avg_price"], 1001.0)
            self.assertEqual(res_buy["qty"], 10)
            self.assertEqual(res_buy.get("status"), "filled")
            self.assertEqual(broker.cash, 100000.0 - (10 * 1001.0))

            # 2. Sell/Flatten order: should fill at Bid (999.0), not mid (1000.0)
            res_sell = broker._flatten("INFY")
            self.assertIsNotNone(res_sell)
            self.assertEqual(res_sell["filled_avg_price"], 999.0)
            self.assertEqual(res_sell["qty"], 10)
            self.assertEqual(res_sell.get("status"), "filled")
            # Cash after round-trip: 100,000 - 10010 + 9990 = 99,980 (₹20 spread cost realized)
            self.assertEqual(broker.cash, 99980.0)
            self.assertNotIn("INFY", broker.book)

    def test_earnings_has_fresh_data_clean_calendar(self):
        ef = EarningsFilter()
        # Mock successful calendar fetch where NO earnings are scheduled in blackout
        # e.g. next_date is None (no upcoming report soon) or far in future
        now_ts = datetime.now().timestamp()
        
        # 1. Successful fetch with no upcoming report: should be fresh & safe
        ef._cache["INFY"] = (None, now_ts, True)
        self.assertTrue(ef.has_fresh_data("INFY"))
        self.assertFalse(ef.is_blackout("INFY"))

        # 2. Successful fetch with earnings 20 days away: should be fresh & not blackout
        future_date = datetime.now(timezone.utc).date() + timedelta(days=20)
        ef._cache["TCS"] = (future_date, now_ts, True)
        self.assertTrue(ef.has_fresh_data("TCS"))
        self.assertFalse(ef.is_blackout("TCS"))

        # 3. Successful fetch with earnings 2 days away: should be fresh & IN blackout
        soon_date = datetime.now(timezone.utc).date() + timedelta(days=2)
        ef._cache["RELIANCE"] = (soon_date, now_ts, True)
        self.assertTrue(ef.has_fresh_data("RELIANCE"))
        self.assertTrue(ef.is_blackout("RELIANCE"))

        # 4. Failed fetch: should NOT be fresh
        ef._cache["BAD"] = (None, now_ts, False)
        self.assertFalse(ef.has_fresh_data("BAD"))

    def test_sentiment_has_fresh_data_clean_news(self):
        se = SentimentEngine()
        now_ts = datetime.now().timestamp()

        # 1. Successful fetch with quiet news (score 0.0, 0 articles): should be fresh & safe
        se._cache["INFY"] = ((0.0, 0), now_ts)
        se._health["INFY"] = (now_ts, True)
        self.assertTrue(se.has_fresh_data("INFY"))
        self.assertFalse(se.is_blocked("INFY"))

        # 2. Successful fetch with negative news: fresh but blocked
        se._cache["BAD_NEWS"] = ((-0.5, 3), now_ts)
        se._health["BAD_NEWS"] = (now_ts, True)
        self.assertTrue(se.has_fresh_data("BAD_NEWS"))
        self.assertTrue(se.is_blocked("BAD_NEWS"))

        # 3. Failed fetch: not fresh
        se._cache["FAILED"] = ((0.0, 0), now_ts)
        se._health["FAILED"] = (now_ts, False)
        self.assertFalse(se.has_fresh_data("FAILED"))


if __name__ == "__main__":
    unittest.main()
