@echo off
cd /d "%~dp0"
title Fengjin AI - Starting...

echo.
echo     =*= Fengjin AI - Cure the Twilight =*=
echo.

REM ============================================================
REM  0. Restart: kill old instances if already running
REM ============================================================
echo  [0/4] Checking for running instances...

powershell -Command ^
  "$c = netstat -ano 2>$null | Select-String ':8765 .*LISTENING';" ^
  "if ($c) {" ^
  "  foreach ($l in $c) {" ^
  "    $p = ($l.ToString().Trim() -split '\s+' | Select-Object -Last 1);" ^
  "    if ($p -eq '0' -or $p -eq '4') { continue }" ^
  "    $name = (Get-Process -Id $p -ErrorAction SilentlyContinue).ProcessName;" ^
  "    if ($name -eq 'python' -or $name -eq 'pythonw') {" ^
  "      try { Stop-Process -Id $p -Force -ErrorAction Stop; Write-Host '  Stopped old backend (PID:' $p ')' } catch {" ^
  "        Write-Host '  WARNING: Unable to stop old backend (PID:' $p ')'" ^
  "      }" ^
  "    } else {" ^
  "      Write-Host '  Port 8765 in use by' $name '(not ours, skipping)'" ^
  "    }" ^
  "  }" ^
  "} else { Write-Host '  No existing backend' }"

REM Also close frontend window
echo  [0/4] Closing old frontend window...
powershell -Command ^
  "$front = Get-Process -Name 'electron' -ErrorAction SilentlyContinue | Where-Object {" ^
  "  $_.MainWindowTitle -like '*风堇*' -or $_.MainWindowTitle -like '*Fengjin*'" ^
  "};" ^
  "if ($front) {" ^
  "  try { $front | Stop-Process -Force -ErrorAction Stop; Write-Host '  Closed frontend window' } catch {" ^
  "    Write-Host '  WARNING: Unable to close frontend'" ^
  "  }" ^
  "} else { Write-Host '  No existing frontend' }"

echo.

REM ============================================================
REM  Offline acceleration: clear proxy + use China mirrors
REM  (only affects this script, not system settings)
REM ============================================================
REM Clear proxy to avoid mirror traffic being routed through VPN
set http_proxy=
set https_proxy=
set HTTP_PROXY=
set HTTPS_PROXY=

REM Use China mirror for Electron binary download
set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
set ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/
set ELECTRON_CACHE=frontend\.cache\electron
if not exist "%ELECTRON_CACHE%" mkdir "%ELECTRON_CACHE%"

REM ============================================================
REM  1. Python venv
REM ============================================================
echo  [1/4] Python virtual environment...
if not exist "venv\Scripts\python.exe" (
    echo    Creating venv...
    python -m venv venv
    if errorlevel 1 (
        echo    ERROR: Failed to create venv. Python 3.10+ required.
        pause
        exit /b 1
    )
)
echo    OK

REM ============================================================
REM  2. Python dependencies
REM ============================================================
echo  [2/4] Python dependencies (first install may take ~3 min)...
REM Only install if requirements.txt changed (or first run)
set NEED_PIP=0
if not exist "venv\.requirements-installed" (
    set NEED_PIP=1
) else (
    powershell -Command ^
      "if ((Get-Item 'requirements.txt').LastWriteTime -gt (Get-Item 'venv\.requirements-installed').LastWriteTime) { exit 1 } else { exit 0 }"
    if errorlevel 1 set NEED_PIP=1
)
if %NEED_PIP%==1 (
    REM Install torch first (GPU or CPU) so requirements.txt won't overwrite it
    nvidia-smi >nul 2>&1
    if errorlevel 1 (
        echo    Installing CPU PyTorch...
        call venv\Scripts\python.exe -m pip install torch==2.6.0 -i https://pypi.tuna.tsinghua.edu.cn/simple --progress-bar on
    ) else (
        echo    Installing CUDA PyTorch...
        call venv\Scripts\python.exe -m pip install torch==2.6.0+cu124 --index-url https://mirrors.nju.edu.cn/pytorch/whl/cu124 --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple --progress-bar on
    )
    REM Install remaining dependencies (torch already installed → pip skips it)
    echo    Installing Python dependencies...
    call venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo    ERROR: Python dependencies failed to install
        pause
        exit /b 1
    )
    echo %date% %time%> "venv\.requirements-installed"
) else (
    REM Even if deps already installed, verify torch matches GPU
    nvidia-smi >nul 2>&1
    if errorlevel 1 (
        rem No GPU — nothing to check
    ) else (
        venv\Scripts\python.exe -c "import torch; exit(0 if torch.cuda.is_available() else 1)" >nul 2>&1
        if errorlevel 1 (
            echo    NVIDIA GPU detected but CPU-only PyTorch found, switching to CUDA PyTorch...
            call venv\Scripts\python.exe -m pip install torch==2.6.0+cu124 --index-url https://mirrors.nju.edu.cn/pytorch/whl/cu124 --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple --force-reinstall --progress-bar on
            echo %date% %time%> "venv\.requirements-installed"
        )
    )
)
echo    OK

REM ============================================================
REM  3. Frontend dependencies
REM ============================================================
echo  [3/4] Frontend dependencies (first install may take ~2 min)...
if not exist "frontend\node_modules\" (
    echo    Installing...
    cd frontend
    call npm install --registry=https://registry.npmmirror.com
    cd ..
    if errorlevel 1 (
        echo    ERROR: npm install failed
        pause
        exit /b 1
    )
)
echo    OK

REM ============================================================
REM  4. Start Electron (LauncherManager spawns backend internally)
REM ============================================================
echo  [4/4] Starting frontend...
start "" /B cmd /c "cd /d frontend && npm run dev >nul 2>&1"
echo    Electron launching...

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

REM Cleanup
echo.
echo    Shutting down...
powershell -Command ^
  "$c = netstat -ano 2>$null | Select-String ':8765 .*LISTENING';" ^
  "if ($c) { foreach ($l in $c) { $p = ($l.ToString().Trim() -split '\s+')[-1]; try { Stop-Process -Id $p -Force -ErrorAction Stop } catch {} } }"
echo    Goodbye.
