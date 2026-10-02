@echo off
rem Weeksir model_admin launcher (no console window). ASCII + CRLF only.
rem Port guard keeps the 1-minute scheduler trigger idempotent: already listening -> exit.
netstat -ano | findstr ":8100" | findstr LISTENING >nul 2>&1 && exit /b 0
cd /d "D:\video-skill"
"D:\video-skill\.venv\Scripts\pythonw.exe" "D:\video-skill\backend\model_admin.py"
