@echo off
REM ============================================================
REM  Debug browser for WeChat Channels live console (CDP 9223)
REM
REM  ASCII-only on purpose: Chinese text inside .cmd depends on the
REM  console codepage (GBK on zh-CN Windows, UTF-8 elsewhere) and
REM  shows up as garbage in most editors. Keep this file ASCII.
REM
REM  After it opens: log in to WeChat Channels, go to the live
REM  console, then minimize the window. DO NOT close it.
REM ============================================================
cd /d "%~dp0"
title Debug Browser - WeChat Channels

start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9223 --user-data-dir="%~dp0browser-profile" https://channels.weixin.qq.com/platform/live/home

echo.
echo   Edge started with remote debugging on port 9223
echo   Profile: %~dp0browser-profile
echo.
echo   Next: log in to WeChat Channels and open the live console.
echo   Verify the CDP port with:
echo     powershell -c "Invoke-RestMethod http://127.0.0.1:9223/json/version"
echo.
timeout /t 8 >nul