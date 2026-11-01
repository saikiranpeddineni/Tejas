@echo off
rem Removes the Tejas auto-start scheduled task and Startup shortcut.
rem Does not stop a running dashboard/bot -- use the dashboard's Stop button for that.

rem =============================================================================
rem [MODIFIED 2026-11-01]: Clean up both Task Scheduler and Windows Startup folder
rem ORIGINAL INTENT: Deleted TejasWatchdog and TejasMarketWake scheduled tasks.
rem REASON FOR CHANGE: We now support dual redundancy via Startup folder as well.
rem SAFE ROLLBACK: Preserved original implementation below as comments.
rem -----------------------------------------------------------------------------
rem [PREVIOUS CODE - PRESERVED]:
rem schtasks /delete /tn "TejasWatchdog" /f
rem schtasks /delete /tn "TejasMarketWake" /f
rem -----------------------------------------------------------------------------
rem [NEW CODE]:
schtasks /delete /tn "TejasWatchdog" /f >nul 2>&1
schtasks /delete /tn "TejasMarketWake" /f >nul 2>&1

set "DIR=%~dp0"
set "DIR=%DIR:~0,-1%"
if exist "%DIR%\.venv\Scripts\python.exe" (
  "%DIR%\.venv\Scripts\python.exe" "%DIR%\autostart_manager.py" uninstall >nul 2>&1
)
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Start Tejas.vbs" (
  del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Start Tejas.vbs" >nul 2>&1
)
rem =============================================================================

echo.
echo Auto-start tasks and startup shortcuts removed.
