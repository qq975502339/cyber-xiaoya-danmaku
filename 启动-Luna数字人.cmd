@echo off
REM ============================================================
REM  Danmaku collector  ->  Luna AI (AI-Vtuber) bridge
REM
REM  Prereq: Luna is running with
REM          platform = "live_monitor_assistant"  (ws 127.0.0.1:5001)
REM  Starts: luna_bridge.py  (no TTS here -- Luna speaks)
REM
REM  Extra args are passed through, e.g.
REM      %~nx0 --only wechat --open-browser
REM      %~nx0 --print-only
REM      %~nx0 --test
REM ============================================================
cd /d %~dp0
title Danmaku -> Luna AI

where py >nul 2>nul
if not errorlevel 1 goto usepy
where python >nul 2>nul
if not errorlevel 1 goto usepython
echo [ERROR] Python not found. Install Python 3.10+ and add it to PATH.
pause
goto :eof

:usepy
py -3 luna_bridge.py %*
goto :end

:usepython
python luna_bridge.py %*
goto :end

:end
if errorlevel 1 pause