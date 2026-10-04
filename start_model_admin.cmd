@echo off
rem Weeksir model_admin launcher (no console window). ASCII + CRLF only.
rem Guard = HTTP probe, NOT netstat: a wedged server keeps the port LISTENING, so
rem "port open" can let the watchdog report success while nothing actually answers.
curl.exe -fs -m 3 -o nul http://127.0.0.1:8100/api/model/caps && exit /b 0
cd /d "D:\video-skill"
rem stderr goes to a log: pythonw has no console, so a real crash used to leave zero evidence
rem (that is why "8100 keeps crashing" could not be confirmed or denied on 2026-10-02).
".venv\Scripts\pythonw.exe" "backend\model_admin.py" 2>>"D:\video-skill\_svc_8100.err.log"
