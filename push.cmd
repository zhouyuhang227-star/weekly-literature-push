@echo off
REM Wrapper so the script can be run by double-click (bypasses ExecutionPolicy).
REM Please keep this file ASCII-only: cmd.exe reads .cmd using the active code page.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0push.ps1" %*
echo.
pause
