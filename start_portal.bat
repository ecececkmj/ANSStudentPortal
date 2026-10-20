@echo off
title Abellana LMS - System Launcher
color 0A

:: Ensure it always starts in the correct project folder
cd /d "C:\Users\Shado\Downloads\webportal project"

echo [INFO] Cleaning up old sessions...
taskkill /F /IM python.exe 2>nul
taskkill /F /IM cloudflared.exe 2>nul
if exist tunnel.log del tunnel.log
if exist result.tmp del result.tmp

echo.
echo [INFO] Starting the local Flask Database Server...
start /B python app.py

echo [INFO] Connecting to Cloudflare...
:: Run Cloudflare in the background and dump all the messy text into a hidden log file
start /B cloudflared tunnel --url http://127.0.0.1:5000 > tunnel.log 2>&1

echo.
echo =========================================================
echo  WAITING FOR LIVE SECURE LINK...
echo =========================================================

:: Loop to check the hidden log file every 1 second until the link appears
:search_link
findstr /C:"trycloudflare.com" tunnel.log > result.tmp 2>nul
set "LIVE_LINK="
for /f "delims=" %%A in (result.tmp) do set "LIVE_LINK=%%A"

if "%LIVE_LINK%"=="" (
    timeout /t 1 /nobreak >nul
    goto search_link
)

:: Clear the screen to strip away all background noise
cls
echo ===================================================
echo    ABELLANA NATIONAL SCHOOL - LMS LAUNCHER
echo ===================================================
echo.
echo  [SYSTEM] Flask Database Server is RUNNING.
echo  [SYSTEM] Cloudflare Tunnel is CONNECTED.
echo.
echo =========================================================
echo  SHARE THIS LINK WITH STUDENTS:
echo.
echo  %LIVE_LINK%
echo =========================================================
echo.
echo  Press the 'X' in the top right to shut down everything.

:: Clean up the temporary file
del result.tmp 2>nul

:: Keep the CMD window open
cmd /k