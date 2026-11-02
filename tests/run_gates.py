"""
run_gates.py — Unlazy Gate Runner
Executes individual suites and emits unambiguous, decisive success tokens.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_regressions import _ensure_optional_imports
_ensure_optional_imports()


def run_suite(target: str, token: str):
    loader = unittest.TestLoader()
    if target == "adaptive":
        import tests.test_adaptive_engine as m
        suite = loader.loadTestsFromModule(m)
    elif target == "regressions":
        import tests.test_regressions as m
        suite = loader.loadTestsFromModule(m)
    elif target == "parity":
        import tests.test_paper_live_parity as m
        suite = loader.loadTestsFromModule(m)
    elif target == "autostart":
        import tests.test_autostart as m
        suite = loader.loadTestsFromModule(m)
    elif target == "alphabot_sync":
        import tests.test_alphabot_sync as m
        suite = loader.loadTestsFromModule(m)
    elif target == "backtest_replay":
        import tests.test_backtest_replay as m
        suite = loader.loadTestsFromModule(m)
    elif target == "discover":
        suite = loader.discover("tests")
    else:
        print(f"Unknown target: {target}")
        sys.exit(1)

    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    if result.wasSuccessful():
        print(token)
        sys.exit(0)
    else:
        print("FAILURES DETECTED")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python run_gates.py <target> <token>")
        sys.exit(1)
    run_suite(sys.argv[1], sys.argv[2])
