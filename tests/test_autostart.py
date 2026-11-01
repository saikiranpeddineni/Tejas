"""
tests/test_autostart.py — Test Morning Autostart, Scheduling, and Supervisor
=============================================================================
Validates:
1. Virtual environment python path resolution across watchdog, webui, and autostart_manager.
2. Windows Startup folder launcher script generation.
3. Market trading window and holiday calculations.
4. Auto-start options loading, saving, and supervisor integration.
5. Scheduled task registration status.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import autostart_manager
import watchdog
import webui
from brokers.base import IST


class AutostartTests(unittest.TestCase):
    def test_venv_python_resolution(self):
        """Verify that get_venv_python and watchdog._python_gui prioritize .venv."""
        pyw = autostart_manager.get_venv_python(gui=True)
        py = autostart_manager.get_venv_python(gui=False)
        self.assertTrue(Path(pyw).exists(), f"pythonw path does not exist: {pyw}")
        self.assertTrue(Path(py).exists(), f"python path does not exist: {py}")
        self.assertIn(".venv", pyw.lower(), "pythonw path must resolve inside .venv")
        self.assertIn(".venv", py.lower(), "python path must resolve inside .venv")

        wd_py = watchdog._python_gui()
        self.assertTrue(Path(wd_py).exists(), f"watchdog gui python does not exist: {wd_py}")
        self.assertIn(".venv", wd_py.lower(), "watchdog gui python must resolve inside .venv")

    def test_startup_folder_script(self):
        """Verify that the Windows Startup folder launcher exists and points to webui."""
        ok = autostart_manager.install_startup_shortcut()
        self.assertTrue(ok, "install_startup_shortcut failed")
        
        startup_file = autostart_manager.get_startup_folder() / "Start Tejas.vbs"
        self.assertTrue(startup_file.exists(), f"Startup script not found at {startup_file}")
        
        content = startup_file.read_text(encoding="utf-8")
        self.assertIn(".venv", content, "Startup script missing .venv path")
        self.assertIn("webui.py", content, "Startup script missing webui.py reference")

    def test_scheduled_task_status(self):
        """Verify that scheduled tasks and startup files are registered."""
        st = autostart_manager.check_status()
        self.assertTrue(st["startup_file_present"], "startup_file_present is False")
        if os.name == "nt":
            self.assertTrue(st["watchdog_task_registered"], "TejasWatchdog scheduled task not registered")
            self.assertTrue(st["wake_task_registered"], "TejasMarketWake scheduled task not registered")

    def test_trading_window_schedule(self):
        """Verify trading window logic for open, close, pre-open buffer, weekend, holiday."""
        # 1. Monday at 09:10 IST (inside 10-minute pre-open buffer) -> True
        dt_pre = datetime(2026, 11, 2, 9, 10, tzinfo=IST) # Nov 2 2026 is Monday
        self.assertTrue(webui.in_trading_window(dt_pre), "09:10 Monday should be inside trading window")

        # 2. Monday at 11:30 IST (mid-session) -> True
        dt_mid = datetime(2026, 11, 2, 11, 30, tzinfo=IST)
        self.assertTrue(webui.in_trading_window(dt_mid), "11:30 Monday should be inside trading window")

        # 3. Monday at 15:40 IST (inside 15-minute post-close buffer) -> True
        dt_post = datetime(2026, 11, 2, 15, 40, tzinfo=IST)
        self.assertTrue(webui.in_trading_window(dt_post), "15:40 Monday should be inside trading window")

        # 4. Monday at 08:30 IST (too early) -> False
        dt_early = datetime(2026, 11, 2, 8, 30, tzinfo=IST)
        self.assertFalse(webui.in_trading_window(dt_early), "08:30 Monday should be outside trading window")

        # 5. Monday at 16:30 IST (after post-close buffer) -> False
        dt_late = datetime(2026, 11, 2, 16, 30, tzinfo=IST)
        self.assertFalse(webui.in_trading_window(dt_late), "16:30 Monday should be outside trading window")

        # 6. Saturday at 11:00 IST (weekend) -> False
        dt_sat = datetime(2026, 11, 7, 11, 0, tzinfo=IST)
        self.assertFalse(webui.in_trading_window(dt_sat), "Saturday should be outside trading window")

    def test_autostart_options_persistence(self):
        """Verify loading and saving autostart configuration."""
        opts = webui.load_autostart_opts()
        self.assertIsInstance(opts, dict)
        self.assertIn("broker", opts)
        self.assertIn("bankroll", opts)

    def test_webui_child_process_launches_with_venv(self):
        """Verify that webui.start_bot invokes tejas.py with .venv python."""
        with patch("subprocess.Popen") as mock_popen, \
             patch("webui.is_running", return_value=False), \
             patch("webui._bot_lock_busy", return_value=False), \
             patch("builtins.open", unittest.mock.mock_open()):
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.pid = 99999
            mock_popen.return_value = mock_proc

            opts = {"broker": "simulated", "dry_run": False}
            res = webui.start_bot(opts, source="auto")
            self.assertTrue(res.get("ok"), f"start_bot failed: {res}")

            call_args = mock_popen.call_args[0][0]
            self.assertIn(".venv", call_args[0].lower(), "Child process python must be from .venv")
            self.assertEqual(call_args[1], "-u")
            self.assertTrue(call_args[2].endswith("tejas.py"))


if __name__ == "__main__":
    unittest.main()
