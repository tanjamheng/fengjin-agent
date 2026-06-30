@echo off
cd /d "%~dp0"
setlocal enabledelayedexpansion
title Fengjin AI - Starting...

echo.
echo     =*= Fengjin AI - Cure the Twilight =*=
echo.

REM ============================================================
REM  0. Restart: kill old backend if already running
REM ============================================================
echo  [0/5] Checking for running instances...

powershell -Command ^
  "$conns = netstat -ano 2>$null | Select-String ':8765 .*LISTENING';" ^
  "if ($conns) {" ^
  "  foreach ($line in $conns) {" ^
  "    $parts = $line.ToString().Trim() -split '\s+';" ^
  "    $pid = $parts[-1];" ^
  "    if ($pid -ne '0' -and $pid -ne '4') {" ^
  "      try {" ^
  "        $proc = Get-Process -Id $pid -ErrorAction Stop;" ^
  "        $name = $proc.ProcessName;" ^
  "        Write-Host ('  Found old backend: ' + $name + ' (PID: ' + $pid + ')');" ^
  "        Stop-Process -Id $pid -Force -ErrorAction Stop;" ^
  "        Write-Host ('  Stopped.');" ^
  "        Start-Sleep -Milliseconds 500;" ^
  "      } catch {" ^
  "        Write-Host ('  WARNING: Port 8765 in use (PID:' + $pid + '), unable to stop');" ^
  "      }" ^
  "    }" ^
  "  }" ^
  "} else {" ^
  "  Write-Host '  No existing instance found';" ^
  "}"

echo.

REM ============================================================
REM  1. Python venv
REM ============================================================
echo  [1/5] Python virtual environment...
if not exist "venv\Scripts\python.exe" (
    echo    Creating venv...
    python -m venv venv
    if errorlevel 1 (
        echo    ERROR: Failed to create venv. Python 3.10+ required.
        pause
        exit /b 1
    )
)
call venv\Scripts\python.exe --version 2>&1 >nul
echo    OK

REM ============================================================
REM  2. Python dependencies
REM ============================================================
echo  [2/5] Python dependencies...
call venv\Scripts\python.exe -m pip install -r requirements.txt -q >nul 2>&1
if errorlevel 1 (
    echo    WARNING: Some dependencies failed to install, continuing...
)
echo    OK

REM ============================================================
REM  3. Frontend dependencies
REM ============================================================
echo  [3/5] Frontend dependencies...
if not exist "frontend\node_modules\" (
    echo    Installing...
    cd frontend
    call npm install >nul 2>&1
    if errorlevel 1 (
        echo    ERROR: npm install failed
        cd ..
        pause
        exit /b 1
    )
    cd ..
)
echo    OK

REM ============================================================
REM  4. Start backend (silent, logs -> logs\app.log)
REM ============================================================
echo  [4/5] Starting backend...
start "" /B venv\Scripts\python.exe -m src.server.server >nul 2>&1

echo    Waiting for models to load (~30s first run)...
powershell -Command ^
  "for ($i=1; $i -le 120; $i++) {" ^
  "  try {" ^
  "    $r = Invoke-RestMethod http://127.0.0.1:8765/health -TimeoutSec 2;" ^
  "    if ($r.status -eq 'ready') { Write-Host ('   Backend ready (' + $i + 's)'); exit 0 }" ^
  "  } catch {}" ^
  "  Start-Sleep 1" ^
  "}; Write-Host '   ERROR: Backend timeout (120s)'; exit 1"

if errorlevel 1 (
    echo.
    echo    ERROR: Backend failed to start. Check logs\app.log
    pause
    exit /b 1
)

REM ============================================================
REM  5. Start frontend (Electron opens its own window)
REM ============================================================
echo  [5/5] Starting frontend...
start "" /B cmd /c "cd /d "%CD%\frontend" && npm run dev >nul 2>&1"

REM ============================================================
REM  Done
REM ============================================================
title Fengjin AI - Running
echo.
echo    ====================================
echo    Fengjin AI is running!
echo.
echo    Backend:  http://127.0.0.1:8765
echo    Logs:     logs\app.log
echo.
echo    Close this window to stop all services.
echo    ====================================
echo.
pause

REM Cleanup: kill backend when user closes this window
powershell -Command ^
  "$conns = netstat -ano 2>$null | Select-String ':8765 .*LISTENING';" ^
  "if ($conns) {" ^
  "  foreach ($line in $conns) {" ^
  "    $parts = $line.ToString().Trim() -split '\s+';" ^
  "    try { Stop-Process -Id $parts[-1] -Force -ErrorAction Stop } catch {}" ^
  "  }" ^
  "}"

echo   Goodbye~
