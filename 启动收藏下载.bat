@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Douyin Favorite Downloader
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto NOPY

python douyin_dl.py %*
set RC=%errorlevel%
echo.
if not "%RC%"=="0" echo [EXIT] code=%RC% -- see messages above
pause
exit /b %RC%

:NOPY
echo [ERROR] python not found. Install Python 3.10+ and check 'Add python.exe to PATH'
pause
exit /b 1
