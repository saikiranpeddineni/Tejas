@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem  setup_autostart.bat  --  run ONCE to enable Tejas auto-start
rem ------------------------------------------------------------
rem  Registers a per-user Windows scheduled task that runs the
rem  watchdog every 5 minutes while you are logged in. The
rem  watchdog launches the Tejas dashboard if it is not already
rem  up; the dashboard then runs the trading bot only during NSE
rem  market hours. Covers reboots, waking from sleep, and crashes.
rem ============================================================

set "DIR=%~dp0"
set "DIR=%DIR:~0,-1%"
set "TASK=TejasWatchdog"

rem =============================================================================
rem [MODIFIED 2026-11-01]: Prioritize .venv python over system PATH
rem ORIGINAL INTENT: Found pythonw via `where pythonw` on PATH.
rem REASON FOR CHANGE: System Python lacks required libraries (joblib, lightgbm),
rem causing crash loops on autostart. Prioritize .venv\\Scripts\\pythonw.exe.
rem SAFE ROLLBACK: Preserved original implementation below as comments.
rem -----------------------------------------------------------------------------
rem [PREVIOUS CODE - PRESERVED]:
rem set "PYW="
rem for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
rem if not defined PYW for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYW set "PYW=%%i"
rem if not defined PYW (
rem   echo [ERROR] Could not find pythonw/python on PATH.
rem   echo         Install Python or add it to PATH, then re-run this file.
rem   pause
rem   exit /b 1
rem )
rem -----------------------------------------------------------------------------
rem [NEW CODE]:
set "PYW="
if exist "%DIR%\.venv\Scripts\pythonw.exe" (
  set "PYW=%DIR%\.venv\Scripts\pythonw.exe"
) else if exist "%DIR%\.venv\Scripts\python.exe" (
  set "PYW=%DIR%\.venv\Scripts\python.exe"
) else (
  for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
  if not defined PYW for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYW set "PYW=%%i"
)
if not defined PYW (
  echo [ERROR] Could not find virtual environment or python on PATH.
  pause
  exit /b 1
)
rem =============================================================================

echo Using Python: !PYW!

rem -- Watchdog task: every 5 min around the clock plus an at-logon trigger.
powershell -NoProfile -Command ^
  "$act = New-ScheduledTaskAction -Execute '!PYW!' -Argument '%DIR%\watchdog.py' -WorkingDirectory '%DIR%';" ^
  "$rep = (New-ScheduledTaskTrigger -Once -At 00:00 -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)).Repetition;" ^
  "$trig = New-ScheduledTaskTrigger -Once -At 00:00;" ^
  "$trig.Repetition = $rep;" ^
  "$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME;" ^
  "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5);" ^
  "Register-ScheduledTask -TaskName '%TASK%' -Action $act -Trigger $trig,$logon -Settings $s -Force | Out-Null"
if errorlevel 1 (
  echo [ERROR] Failed to register the scheduled task.
  pause
  exit /b 1
)

rem -- Pre-open wake task: weekdays 08:58 IST, wakes a SLEEPING PC for the NSE open.
powershell -NoProfile -Command ^
  "$act  = New-ScheduledTaskAction -Execute '!PYW!' -Argument '%DIR%\watchdog.py' -WorkingDirectory '%DIR%';" ^
  "$trig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 08:58;" ^
  "$ws   = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5);" ^
  "Register-ScheduledTask -TaskName 'TejasMarketWake' -Action $act -Trigger $trig -Settings $ws -Force | Out-Null"

rem -- Allow RTC wake timers on AC and battery (needed for WakeToRun to fire).
powershell -NoProfile -Command ^
  "powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1;" ^
  "powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1;" ^
  "powercfg /setactive SCHEME_CURRENT"

rem -- Register Windows Startup folder launcher as defense-in-depth
if exist "%DIR%\.venv\Scripts\python.exe" (
  "%DIR%\.venv\Scripts\python.exe" "%DIR%\autostart_manager.py" install
)

echo.
echo Registered task "%TASK%" (every 5 min, while logged in).
echo Registered task "TejasMarketWake" (weekdays 08:58, wakes the PC from sleep).
echo Registered Startup shortcut in Windows Startup folder.
echo.
echo Done. The dashboard will keep itself running; the bot trades
echo only during NSE hours. Use the dashboard's Stop button to
echo pause it (it will stay down until you press Start again).
