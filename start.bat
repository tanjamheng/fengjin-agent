@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   =*= Fengjin AI - Cure the Twilight =*=
echo   ======================================
echo.
echo   Starting backend + frontend...
echo.
powershell -ExecutionPolicy Bypass -File "%~dp0start.ps1"
pause
