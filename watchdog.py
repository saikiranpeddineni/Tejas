"""
watchdog.py — keep the Tejas dashboard alive whenever the PC is on.
===================================================================
A *one-shot* check, designed to be run every few minutes by Windows Task
Scheduler (see setup_autostart.bat):

  • If the dashboard (webui.py) is already answering on its port → do nothing.
  • Otherwise → launch it hidden (pythonw) and exit.

The dashboard itself owns the market-hours logic: its supervisor thread starts
the trading bot only inside NSE windows and stops it after close. So this
watchdog only has to guarantee the dashboard is running — it covers reboots
(Task Scheduler relaunches after logon) and crashes (the periodic re-check).
"""
import os
import socket
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8765  # must match HOST, PORT in webui.py

# NOTE: deliberately stdlib-only with NO project imports. This runs headless
# under Task Scheduler with output discarded — an import-time failure anywhere
# in the project (bad .env value, missing dependency in the pythonw interpreter)
# would silently kill the entire keep-alive chain. The dashboard runs 24/7 (so
# P&L stays viewable after close); only the BOT is market-hours-gated, and the
# dashboard's supervisor owns that logic.


def _dashboard_up() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _python_gui() -> str:
    """pythonw.exe (no console window) if available, else the current python."""
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def main() -> None:
    if _dashboard_up():
        return  # already running — nothing to do
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    # Background launch: no console window, and no browser tab (must never pop
    # to the foreground / appear during screen sharing). Open the dashboard
    # yourself at http://127.0.0.1:8765 when you want to view it.
    env = dict(os.environ)
    env["TEJAS_NO_BROWSER"] = "1"
    startup_log_path = os.path.join(BASE_DIR, "tejas.startup.log")
    try:
        # Keep import/bind failures diagnosable when Task Scheduler launches us
        # without a console.  The file is ignored as runtime state and does not
        # contain broker credentials; cap it so a crash loop cannot fill disk.
        if os.path.exists(startup_log_path) and os.path.getsize(startup_log_path) > 1024 * 1024:
            with open(startup_log_path, "wb"):
                pass
        startup_log = open(startup_log_path, "ab")
        try:
            subprocess.Popen(
                [_python_gui(), os.path.join(BASE_DIR, "webui.py")],
                cwd=BASE_DIR, env=env,
                stdout=subprocess.DEVNULL, stderr=startup_log,
                creationflags=flags,
            )
        finally:
            startup_log.close()
    except OSError:
        # A transient launch failure is retried by the next scheduled watchdog
        # invocation; leave a concise diagnostic without dumping environment data.
        try:
            with open(startup_log_path, "ab") as f:
                f.write(b"watchdog: failed to launch dashboard\n")
        except OSError:
            pass


if __name__ == "__main__":
    main()
