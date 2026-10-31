@echo off
REM Run the FOFA browser lane from any working directory (Windows host).
cd /d "%~dp0..\.."
set "PYTHONUNBUFFERED=1"
if exist ".venv\Scripts\python.exe" (
  set "PYTHON=.venv\Scripts\python.exe"
) else (
  set "PYTHON=python"
)
"%PYTHON%" scripts\ops\fofa_browser.py %*
exit /b %errorlevel%
