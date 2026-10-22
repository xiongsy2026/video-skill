@echo off
rem Weeksir 8090 (D:\video-skill\app.py) launcher. ASCII + CRLF only.
rem Guard = HTTP probe, NOT netstat: a wedged server keeps the port LISTENING, so
rem "port open" can let a watchdog report success while nothing actually answers.
curl.exe -fs -m 5 -o nul http://127.0.0.1:8090/ && exit /b 0
cd /d "D:\video-skill"
rem pythonw (no console) => sys.stdout is None => the bare print() calls inside
rem pipeline/step5_video_generate.py (BOUNDARY-protected, call-only) become no-ops
rem instead of killing a running job. On 2026-10-09 tvc Step3 died twice within
rem seconds with: 'gbk' codec can't encode character '\u2713' in position 2.
rem PYTHONIOENCODING is belt-and-braces for anyone who starts python.exe by hand.
rem 2026-10-22: source identity for the spend-approval gate. Only the workbench process
rem (this launcher) is allowed to skip the human approval for video.generate, because a
rem human pressed "submit" here. DSH agent sessions start without this var and still need a
rem grant, so their approval link stays load-bearing. Do NOT move this into code or params:
rem self-reported identity is display-only, never a safety check.
set "WEEKSIR_ENTRY=workbench"
set "PYTHONIOENCODING=utf-8:replace"
"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" -u app.py 1>>"D:\video-skill\_svc_8090.out.log" 2>>"D:\video-skill\_svc_8090.err.log"
rem WHY python.exe and NOT pythonw (2026-10-10, Qoder): pythonw leaves sys.stderr = None, and uvicorn's
rem default LOGGING_CONFIG attaches a StreamHandler to sys.stderr. Started detached (Start-Process or a
rem scheduled task, no inherited console handles) it dies at boot with
rem   ValueError: Unable to configure formatter 'default'  caused by  'NoneType' object has no attribute 'isatty'
rem It only ever "worked" from an interactive shell because the console handles were inherited there.
rem The bare-print cp936 killer (tvc Step3 died twice on U+2713) is handled honestly now: streams go to
rem log files and PYTHONIOENCODING=utf-8:replace above makes unencodable chars lossy instead of raising.
