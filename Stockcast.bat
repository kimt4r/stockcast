@echo off
setlocal
title Stockcast
cd /d "%~dp0"

set "STOCKCAST_PYTHON="

if exist ".venv\Scripts\python.exe" (
    set "STOCKCAST_PYTHON=%~dp0.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul
    if not errorlevel 1 set "STOCKCAST_PYTHON=py"

    if not defined STOCKCAST_PYTHON (
        where python >nul 2>nul
        if not errorlevel 1 set "STOCKCAST_PYTHON=python"
    )
)

if not defined STOCKCAST_PYTHON (
    echo.
    echo [ERROR] Python 3.11 or newer is required.
    echo Install Python, then run this file again.
    echo.
    pause
    exit /b 1
)

set "STOCKCAST_OLD_SERVER="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"127.0.0.1:8787 .*LISTENING"') do (
    set "STOCKCAST_OLD_SERVER=1"
    echo Stopping the previous Stockcast server ^(PID %%P^)...
    taskkill /PID %%P /F >nul 2>nul
)

if defined STOCKCAST_OLD_SERVER timeout /t 2 /nobreak >nul

netstat -ano | findstr /R /C:"127.0.0.1:8787 .*LISTENING" >nul
if not errorlevel 1 (
    echo.
    echo [ERROR] The previous Stockcast server could not be stopped.
    echo Close all Stockcast windows or end the python.exe process in Task Manager.
    echo Then run this file again.
    echo.
    pause
    exit /b 1
)

"%STOCKCAST_PYTHON%" -c "import flask, requests, stockcast" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [SETUP NEEDED] Stockcast is not installed in this Python environment.
    echo Run the following command once in this folder:
    echo.
    echo     "%STOCKCAST_PYTHON%" -m pip install -e .
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Stockcast is starting at http://127.0.0.1:8787
echo   Keep this window open while using Stockcast.
echo   Close this window or press Ctrl+C to stop Stockcast.
echo ============================================================
echo.

start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8787'"
"%STOCKCAST_PYTHON%" -m stockcast.web

echo.
echo Stockcast has stopped.
pause
