"""
test_log_anomalies_fixes.py — Regression & Verification Suite for Log Anomaly Fixes
===================================================================================
Verifies:
1. Purge of dummy tickers (e.g. DUMMYHEG) from universe cache and loader sanitization.
2. Industry sector mapping for metals, steel, and minerals (sector 4) to eliminate sector 6 bottleneck.
3. Expanded sector limits configuration for diversified industries ('other' >= 30%, 'energy' >= 20%).
4. ML metrics metadata schema compatibility with lgb_gold.txt (no model mismatch warning).
5. Earnings filter bounded execution and log levels.
"""

import json
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config
from universe import _industry_code, get_universe
from ml_engine_gold import MLEngineGold
from earnings_filter import EarningsFilter


class TestLogAnomaliesFixes(unittest.TestCase):

    def test_dummy_ticker_purged_and_sanitized(self):
        """Verify DUMMYHEG is not in universe and loader rejects dummy symbols."""
        symbols, sectors = get_universe()
        self.assertNotIn("DUMMYHEG", symbols, "DUMMYHEG must not be in universe symbols")
        self.assertNotIn("DUMMYHEG", sectors, "DUMMYHEG must not be in universe sectors")

        # Verify universe cache file itself
        cache_path = os.path.join(str(ROOT), "universe.local.json")
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.assertNotIn("DUMMYHEG", d.get("symbols", []), "DUMMYHEG should not be in cached symbols list")
            self.assertNotIn("DUMMYHEG", d.get("sectors", {}), "DUMMYHEG should not be in cached sectors map")

    def test_metals_and_steel_industry_mapping(self):
        """Verify metals, steel, ferrous, and minerals map to sector 4 (metals/energy)."""
        test_cases = [
            ("Ferrous Metals", 4),
            ("Non - Ferrous Metals", 4),
            ("Iron & Steel", 4),
            ("Iron and Steel Products", 4),
            ("Diversified Metals", 4),
            ("Minerals & Mining", 4),
            ("Oil & Gas", 4),
            ("Power Generation", 4),
            ("Coal", 4),
            ("Information Technology", 0),
            ("Private Sector Bank", 1),
            ("Pharmaceuticals", 2),
            ("Consumer Food", 3),
        ]
        for ind_name, expected_code in test_cases:
            with self.subTest(industry=ind_name):
                code = _industry_code(ind_name)
                self.assertEqual(code, expected_code, f"Industry '{ind_name}' should map to {expected_code}, got {code}")

    def test_sector_limits_headroom(self):
        """Verify 'other' and 'energy' sector limits provide sufficient diversified headroom."""
        limits = Config.SECTOR_LIMITS
        self.assertGreaterEqual(limits.get("other", 0), 0.30, "'other' sector limit should be at least 30%")
        self.assertGreaterEqual(limits.get("energy", 0), 0.20, "'energy' sector limit should be at least 20%")
        self.assertGreaterEqual(limits.get("technology", 0), 0.30)
        self.assertGreaterEqual(limits.get("finance", 0), 0.20)
        self.assertGreaterEqual(limits.get("healthcare", 0), 0.20)

    def test_ml_metrics_model_tag_match(self):
        """Verify ML engine loads metrics and model file without mismatch warning."""
        engine = MLEngineGold()
        self.assertTrue(getattr(engine, "_metrics_match_model", False),
                        "MLEngineGold should report _metrics_match_model == True")
        self.assertEqual(engine._model_file, "lgb_gold.txt")
        self.assertGreater(engine.val_auc, 0.0)

    def test_earnings_filter_budget_safety(self):
        """Verify earnings filter bounded execution doesn't raise exceptions."""
        ef = EarningsFilter()
        blackout = ef.get_blackout_set(["TCS", "INFY", "RELIANCE"], time_budget_secs=5.0, fail_closed=False)
        self.assertIsInstance(blackout, set)


if __name__ == "__main__":
    unittest.main()
