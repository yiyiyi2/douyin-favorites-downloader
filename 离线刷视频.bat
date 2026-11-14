@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Douyin Offline Player
cd /d "%~dp0"

rem Pure offline mode: only the local panel + player.
rem No internet, no Chrome, no downloading.
rem Keep every rem line ASCII-only: cmd.exe parses a .bat with the
rem system ANSI codepage (GBK) whatever chcp says, so UTF-8 Chinese
rem inside a rem line byte-shifts and gets executed as a command.
rem Chinese is fine inside echo (passed through verbatim).

where pythonw >nul 2>nul
if errorlevel 1 goto TRYPY
start "" pythonw douyin_dl.py --offline %*
echo [OK] 离线播放器启动中, 浏览器马上会打开...
echo      没自动打开就手动访问: http://127.0.0.1:8899/player
timeout /t 3 >nul
start "" http://127.0.0.1:8899/player
exit /b 0

:TRYPY
where python >nul 2>nul
if errorlevel 1 goto NOPY
python douyin_dl.py --offline %*
pause
exit /b %errorlevel%

:NOPY
echo [ERROR] python not found. Install Python 3.10+ with Add to PATH.
pause
exit /b 1
