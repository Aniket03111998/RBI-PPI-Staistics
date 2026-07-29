@echo off
REM Runs the monthly RBI PPI data refresh. Intended for Windows Task Scheduler.
REM Logs to refresh_log.txt in the repo root.
cd /d "%~dp0.."
"C:\Users\Aniket\AppData\Local\Programs\Python\Python312\python.exe" -m ppi.refresh >> refresh_log.txt 2>&1
