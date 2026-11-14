@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Douyin Offline Player
cd /d "%~dp0"

rem 纯离线模式: 只起本地面板 + 播放器, 完全不联网、不碰浏览器、不下载。
rem 想刷已经下好的视频时双击这个, 没网也能用。

where pythonw >nul 2>nul
if errorlevel 1 goto TRYPY
start "" pythonw douyin_dl.py --offline %*
echo [OK] 离线播放器启动中, 浏览器马上会打开...
echo      如果没自动打开, 手动访问: http://127.0.0.1:8899/player
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
echo [ERROR] 没找到 Python。请先装 Python 3.10+ 并勾选 Add python.exe to PATH
pause
exit /b 1
