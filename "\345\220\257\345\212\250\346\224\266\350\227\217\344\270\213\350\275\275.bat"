@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Douyin Favorite Downloader
cd /d "%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 goto TRYPY
rem guardian mode: auto restart if crashed. This window closes itself in a few seconds.
start "" pythonw douyin_dl/daemon.py %*
echo [OK] starting... panel will open in browser shortly.
echo      log: daemon.log / douyin_dl.log   (stop: use Task Manager on processes of douyin_dl.py / daemon.py)
rem wait for panel to be ready, then open it in default browser
timeout /t 4 >nul
start "" http://127.0.0.1:8899/
exit /b 0

:TRYPY
where python >nul 2>nul
if errorlevel 1 goto NOPY
python douyin_dl/daemon.py %*
pause
exit /b %errorlevel%

:NOPY
echo [ERROR] python not found. Install Python 3.10+ and check 'Add python.exe to PATH'
pause
exit /b 1
