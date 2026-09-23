@echo off
REM One hourly cycle of the document intake service.
REM
REM Registered with Windows Task Scheduler (see scripts/schedule.md). Everything it does is
REM resumable and bounded, so a cycle that is interrupted or falls behind leaves the remainder
REM for the next hour rather than losing it.
REM
REM This NEVER uploads to TransportPro. `intake file --execute` is deliberately not reachable
REM from here - an unattended job that can upload is one that can upload the wrong thing at 3am.

setlocal
set PROJECT=%~dp0..
cd /d "%PROJECT%"

if not exist "logs" mkdir "logs"
for /f "tokens=1-3 delims=/ " %%a in ("%DATE%") do set TODAY=%%c-%%a-%%b

".venv\Scripts\python.exe" -m intake cycle --max-spend 2.0 >> "logs\cycle-%TODAY%.log" 2>&1
set RC=%ERRORLEVEL%

REM Non-zero propagates to Task Scheduler's "Last Run Result" so a bad cycle is visible
REM in the console without anyone reading a log file.
exit /b %RC%
