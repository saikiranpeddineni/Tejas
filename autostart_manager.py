"""
autostart_manager.py — Morning Autostart & Scheduled Tasks Management
=====================================================================
Ensures Tejas automatically launches whenever the user's laptop opens
in the morning, logs in, or wakes from sleep before NSE market open.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, Optional

log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


def get_venv_python(gui: bool = True) -> str:
    """Returns absolute path to virtual environment pythonw (GUI) or python (CLI)."""
    scripts_dir = BASE_DIR / ".venv" / "Scripts"
    if gui:
        pyw = scripts_dir / "pythonw.exe"
        if pyw.exists():
            return str(pyw)
    py = scripts_dir / "python.exe"
    if py.exists():
        return str(py)
    
    # Fallback to sys.executable or adjacent pythonw
    if gui:
        cand = Path(sys.executable).parent / "pythonw.exe"
        if cand.exists():
            return str(cand)
    return sys.executable


def get_startup_folder() -> Path:
    """Returns path to user's Windows Startup directory."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        p = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        if p.exists():
            return p
    # Fallback default
    home = Path.home()
    return home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def install_startup_shortcut() -> bool:
    """Creates a startup script in the Windows Startup directory."""
    try:
        startup_dir = get_startup_folder()
        startup_dir.mkdir(parents=True, exist_ok=True)
        target_vbs = startup_dir / "Start Tejas.vbs"
        
        pyw_exe = get_venv_python(gui=True)
        webui_py = str(BASE_DIR / "webui.py")
        
        vbs_content = (
            "' Tejas Morning Autostart Launcher\n"
            "Set fso = CreateObject(\"Scripting.FileSystemObject\")\n"
            "Set sh  = CreateObject(\"WScript.Shell\")\n"
            f"dir = \"{BASE_DIR}\"\n"
            "sh.CurrentDirectory = dir\n"
            f"sh.Run \"\"\"{pyw_exe}\"\" \"\"{webui_py}\"\"\", 0, False\n"
        )
        target_vbs.write_text(vbs_content, encoding="utf-8")
        log.info(f"Installed startup launcher to {target_vbs}")
        return True
    except Exception as e:
        log.error(f"Failed to install startup launcher: {e}")
        return False


def install_scheduled_tasks() -> Dict[str, bool]:
    """Registers TejasWatchdog and TejasMarketWake in Windows Task Scheduler."""
    results = {"watchdog": False, "wake": False}
    if os.name != "nt":
        log.warning("Scheduled tasks are only supported on Windows.")
        return results

    pyw = get_venv_python(gui=True)
    watchdog_script = str(BASE_DIR / "watchdog.py")
    dir_str = str(BASE_DIR)

    # 1. TejasWatchdog: Run at logon + every 5 minutes around the clock
    ps_watchdog = f"""
    $act = New-ScheduledTaskAction -Execute '{pyw}' -Argument '{watchdog_script}' -WorkingDirectory '{dir_str}';
    $rep = (New-ScheduledTaskTrigger -Once -At 00:00 -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)).Repetition;
    $trig = New-ScheduledTaskTrigger -Once -At 00:00;
    $trig.Repetition = $rep;
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME;
    $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5);
    Register-ScheduledTask -TaskName 'TejasWatchdog' -Action $act -Trigger $trig,$logon -Settings $s -Force | Out-Null
    """

    try:
        cp = subprocess.run(["powershell", "-NoProfile", "-Command", ps_watchdog], capture_output=True, text=True, timeout=15)
        results["watchdog"] = (cp.returncode == 0)
    except Exception as e:
        log.error(f"Failed registering TejasWatchdog: {e}")

    # 2. TejasMarketWake: Weekdays at 08:58 IST, wakes PC from sleep before market opens
    ps_wake = f"""
    $act  = New-ScheduledTaskAction -Execute '{pyw}' -Argument '{watchdog_script}' -WorkingDirectory '{dir_str}';
    $trig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 08:58;
    $ws   = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5);
    Register-ScheduledTask -TaskName 'TejasMarketWake' -Action $act -Trigger $trig -Settings $ws -Force | Out-Null
    """

    try:
        cp = subprocess.run(["powershell", "-NoProfile", "-Command", ps_wake], capture_output=True, text=True, timeout=15)
        results["wake"] = (cp.returncode == 0)
    except Exception as e:
        log.error(f"Failed registering TejasMarketWake: {e}")

    # Enable RTC wake timers on AC and DC so WakeToRun works
    try:
        subprocess.run(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE", "1"], capture_output=True, timeout=5)
        subprocess.run(["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE", "1"], capture_output=True, timeout=5)
        subprocess.run(["powercfg", "/setactive", "SCHEME_CURRENT"], capture_output=True, timeout=5)
    except Exception:
        pass

    return results


def check_status() -> Dict[str, Any]:
    """Inspects the autostart status of Tejas."""
    status: Dict[str, Any] = {
        "python_gui": get_venv_python(gui=True),
        "python_cli": get_venv_python(gui=False),
        "startup_file_present": False,
        "watchdog_task_registered": False,
        "wake_task_registered": False,
    }

    startup_file = get_startup_folder() / "Start Tejas.vbs"
    status["startup_file_present"] = startup_file.exists()

    if os.name == "nt":
        try:
            cp1 = subprocess.run(["schtasks", "/query", "/tn", "TejasWatchdog"], capture_output=True, timeout=5)
            status["watchdog_task_registered"] = (cp1.returncode == 0)
        except Exception:
            pass

        try:
            cp2 = subprocess.run(["schtasks", "/query", "/tn", "TejasMarketWake"], capture_output=True, timeout=5)
            status["wake_task_registered"] = (cp2.returncode == 0)
        except Exception:
            pass

    return status


def uninstall() -> Dict[str, bool]:
    """Removes all autostart configurations."""
    results = {"startup_removed": False, "watchdog_deleted": False, "wake_deleted": False}
    startup_file = get_startup_folder() / "Start Tejas.vbs"
    if startup_file.exists():
        try:
            startup_file.unlink()
            results["startup_removed"] = True
        except Exception:
            pass

    if os.name == "nt":
        try:
            cp = subprocess.run(["schtasks", "/delete", "/tn", "TejasWatchdog", "/f"], capture_output=True, timeout=5)
            results["watchdog_deleted"] = (cp.returncode == 0)
        except Exception:
            pass

        try:
            cp = subprocess.run(["schtasks", "/delete", "/tn", "TejasMarketWake", "/f"], capture_output=True, timeout=5)
            results["wake_deleted"] = (cp.returncode == 0)
        except Exception:
            pass

    return results


if __name__ == "__main__":
    action = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if action == "install":
        s_ok = install_startup_shortcut()
        t_ok = install_scheduled_tasks()
        print(f"Startup shortcut installed: {s_ok}")
        print(f"Scheduled tasks: {t_ok}")
    elif action == "uninstall":
        res = uninstall()
        print(f"Uninstall results: {res}")
    else:
        st = check_status()
        print("Tejas Autostart Status:")
        for k, v in st.items():
            print(f"  {k}: {v}")
