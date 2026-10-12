@echo off
call "%~dp0deploy\startup.bat" %*
exit /b %errorlevel%
