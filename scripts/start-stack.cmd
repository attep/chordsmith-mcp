@echo off
rem Start (or repair) the whole ChordSmith stack after a reboot.
rem Reads the quick-tunnel URL, updates .env, recreates the server when the URL changed and
rem mints a manager token when needed. See docs\startup.md for details.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-stack.ps1" %*
echo.
pause
