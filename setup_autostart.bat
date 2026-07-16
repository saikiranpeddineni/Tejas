@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem  setup_autostart.bat  --  run ONCE to enable Tejas auto-start
rem ------------------------------------------------------------
rem  Registers a per-user Windows scheduled task that runs the
rem  watchdog every 5 minutes while you are logged in. The
rem  watchdog launches the Tejas dashboard if it is not already
rem  up; the dashboard then runs the trading bot only during NSE
rem  market hours. Covers reboots and crashes. No admin needed.
rem ============================================================

set "DIR=%~dp0"
set "DIR=%DIR:~0,-1%"
set "TASK=TejasWatchdog"

rem -- locate pythonw.exe (hidden, no console); fall back to python.exe --
set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
  echo [ERROR] Could not find pythonw/python on PATH.
  echo         Install Python or add it to PATH, then re-run this file.
  pause
  exit /b 1
)
echo Using Python: !PYW!

rem -- Watchdog task: every 5 min around the clock plus an at-logon trigger.
rem    The DASHBOARD stays up 24/7 so P&L/trade history are viewable after
rem    close; the dashboard's supervisor starts/stops the BOT for market hours.
rem    (No WakeToRun here: a 5-min repeating task must NOT wake the PC all night.)
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

rem -- Pre-open wake task: weekdays 08:58 IST, wakes a SLEEPING PC for the NSE
rem    open. Once the bot starts it holds a keep-awake until the 15:45 stop.
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

echo.
echo Registered task "%TASK%" (every 5 min, while logged in).
echo Registered task "TejasMarketWake" (weekdays 08:58, wakes the PC from sleep).
echo Launching the dashboard now...
schtasks /run /tn "%TASK%" >nul 2>&1
echo.
echo Done. The dashboard will keep itself running; the bot trades
echo only during NSE hours. Use the dashboard's Stop button to
echo pause it (it will stay down until you press Start again).
pause
