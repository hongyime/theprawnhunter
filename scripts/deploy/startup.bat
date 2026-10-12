@echo off
REM TheprawnHunter startup script
REM Auto-placed in Windows Startup folder — runs on every login
REM Waits for Docker Desktop to be ready, starts containers, clears stale leases

set LOGFILE=C:\theprawnhunter\scripts\deploy\startup.log
echo [%date% %time%] TheprawnHunter startup triggered >> %LOGFILE%

REM Wait for Docker Desktop engine (up to 120s, check every 5s)
set RETRIES=24
:WAIT_LOOP
docker info >nul 2>&1
if %errorlevel% == 0 goto DOCKER_READY
set /a RETRIES=%RETRIES%-1
if %RETRIES% == 0 goto DOCKER_TIMEOUT
echo [%date% %time%] Waiting for Docker... (%RETRIES% retries left) >> %LOGFILE%
timeout /t 5 /nobreak >nul
goto WAIT_LOOP

:DOCKER_TIMEOUT
echo [%date% %time%] ERROR: Docker not ready after 120s >> %LOGFILE%
exit /b 1

:DOCKER_READY
echo [%date% %time%] Docker ready. Starting containers... >> %LOGFILE%
cd /d C:\theprawnhunter
docker compose up -d >> %LOGFILE% 2>&1
echo [%date% %time%] docker compose up -d done (exit %errorlevel%) >> %LOGFILE%

REM Wait a bit then clear stale session leases
timeout /t 30 /nobreak >nul
echo [%date% %time%] Clearing stale session leases... >> %LOGFILE%
python C:\theprawnhunter\scripts\deploy\post_startup.py >> %LOGFILE% 2>&1
echo [%date% %time%] Startup complete. >> %LOGFILE%
exit /b 0
