@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Douyin Favorite Downloader (Guardian)
cd /d "%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 goto TRYPY
rem 守护模式: pythonw 无黑窗口, 下载进程挂了自动拉起; 想看日志就开 downloads 同级的 douyin_dl.log
start "" pythonw douyin_dl/daemon.py
echo [OK] guardian started in background. Panel: http://127.0.0.1:8899/
echo      log: douyin_dl.log   (close: taskkill /f /im pythonw.exe)
timeout /t 3 >nul
exit /b 0

:TRYPY
where python >nul 2>nul
if errorlevel 1 goto NOPY
python douyin_dl/daemon.py
pause
exit /b %errorlevel%

:NOPY
echo [ERROR] python not found. Install Python 3.10+ and check 'Add python.exe to PATH'
pause
exit /b 1
