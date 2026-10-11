@echo off
rem Weeksir 8100 watchdog tick (hidden: run_hidden.vbs wraps this; task = WeeksirModelAdmin).
rem ONE line on purpose. The whole rule (probe -> kill the wedged holder -> start -> re-verify)
rem lives in svc_8100_guard.py and is pinned by C:\dsh\scripts\check-svc-8100.py.
rem Two earlier shapes failed in the field on 2026-10-11:
rem   * "curl probe && exit 0" then start  -> a wedged-but-LISTENING server made it start a
rem     second instance that could never bind (a zombie with no port at all).
rem   * "guard sweep" + "start" on two lines -> line 2 waited for line 1; a stuck sweep meant
rem     8100 simply never came back.
rem The guard must also never call "schtasks /run": this file IS the task action, so that is
rem self-recursion and it kills whatever it just brought up.
cd /d "D:\video-skill"
".venv\Scripts\pythonw.exe" "svc_8100_guard.py" ensure 2>>"D:\video-skill\_svc_8100.err.log"
exit /b %ERRORLEVEL%
