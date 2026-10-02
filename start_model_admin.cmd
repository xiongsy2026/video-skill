@echo off
rem Weeksir model_admin launcher (no console window). ASCII + CRLF only.
rem Port guard keeps the 1-minute scheduler trigger idempotent: already listening -> exit.
netstat -ano | findstr ":8100" | findstr LISTENING >nul 2>&1 && exit /b 0
cd /d "D:\video-skill"
rem stderr goes to a log: pythonw has no console, so a real crash used to leave zero evidence
rem (that is why "8100 keeps crashing" could not be confirmed or denied on 2026-10-02).
"D:\video-skill\.venv\Scripts\pythonw.exe" "D:\video-skill\backend\model_admin.py" 2>>"D:\video-skill\_svc_8100.err.log"
