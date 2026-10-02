@echo off
rem Weeksir video-ui (:8765) launcher, no console window. Called by scheduled task WeeksirVideoUI.
rem Guard: if something already listens on 8765, exit quietly (no double start, no restart spin).
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul 2>&1 && exit /b 0
cd /d "C:\dsh\scratch\video-ui\video-ui"
rem RUNS root must be explicit: store.py defaults to a path that does not exist on this box.
set "VIDEO_UI_RUNS=C:\dsh\video_runs"
set "VIDEO_UI_PORT=8765"
"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\pythonw.exe" app.py
