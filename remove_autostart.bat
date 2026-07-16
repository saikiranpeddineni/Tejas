@echo off
rem Removes the Tejas auto-start scheduled task. Does not stop a
rem running dashboard/bot -- use the dashboard's Stop button for that.
schtasks /delete /tn "TejasWatchdog" /f
schtasks /delete /tn "TejasMarketWake" /f
echo.
echo Auto-start task removed.
pause
