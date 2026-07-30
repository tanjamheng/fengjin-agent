@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 (
    echo ERROR: Unable to enter the project directory: %~dp0
    pause
    exit /b 1
)
title Fengjin AI - Starting...

if not exist "logs\" mkdir "logs" >nul 2>&1
if not exist "logs\" (
    echo ERROR: Unable to create the logs directory. Check folder permissions.
    pause
    exit /b 1
)
set "STARTUP_LOG=%CD%\logs\startup.log"
> "%STARTUP_LOG%" echo [%date% %time%] Fengjin startup begins
call :log "Project root: %CD%"

REM ============================================================
REM  Preflight: fail visibly before stopping a healthy old instance
REM ============================================================
for %%F in ("requirements.txt" "frontend\package.json") do (
    if not exist %%F (
        set "FAIL_REASON=Missing required file: %%~F"
        goto :fatal
    )
)

where powershell.exe >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=PowerShell is unavailable or blocked by system policy"
    goto :fatal
)

where python.exe >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=Python was not found. Install Python 3.10+ and add it to PATH"
    goto :fatal
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=Python 3.10+ is required, or the Python command is not usable"
    goto :fatal
)
for /f "delims=" %%V in ('python --version 2^>^&1') do call :log "%%V"

where node.exe >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=Node.js was not found. Install Node.js 18+ and restart Explorer"
    goto :fatal
)
node -e "process.exit(Number(process.versions.node.split('.')[0]) >= 18 ? 0 : 1)" >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=Node.js 18+ is required"
    goto :fatal
)
where npm.cmd >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=npm was not found. Repair the Node.js installation"
    goto :fatal
)
for /f "delims=" %%V in ('node --version 2^>^&1') do call :log "Node %%V"
for /f "delims=" %%V in ('npm --version 2^>^&1') do call :log "npm %%V"

echo.
echo     =*= Fengjin AI - Cure the Twilight =*=
echo.

REM ============================================================
REM  0. Restart: kill old instances if already running
REM ============================================================
echo  [0/4] Checking for running instances...
call :log "Checking for running instances"

call :stop_tracked_backend
call :log "Tracked backend cleanup completed"

REM Also close frontend window
echo  [0/4] Closing old frontend window...
powershell.exe -NoProfile -NonInteractive -Command ^
  "$front = Get-Process -Name 'electron' -ErrorAction SilentlyContinue | Where-Object {" ^
  "  $_.MainWindowTitle -like '*风堇*' -or $_.MainWindowTitle -like '*Fengjin*'" ^
  "};" ^
  "if ($front) {" ^
  "  try { $front | Stop-Process -Force -ErrorAction Stop; Write-Host '  Closed frontend window' } catch {" ^
  "    Write-Host '  WARNING: Unable to close frontend'" ^
  "  }" ^
  "} else { Write-Host '  No existing frontend' }"
call :log "Existing frontend cleanup completed"

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
set "ELECTRON_CACHE=%CD%\frontend\.cache\electron"
REM Never inherit CI/user settings that intentionally skip Electron's binary.
set "ELECTRON_SKIP_BINARY_DOWNLOAD="
if not exist "%ELECTRON_CACHE%" mkdir "%ELECTRON_CACHE%"

REM ============================================================
REM  1. Python venv
REM ============================================================
echo  [1/4] Python virtual environment...
if not exist "venv\Scripts\python.exe" (
    echo    Creating venv...
    call :log "Creating Python virtual environment"
    python -m venv venv >> "%STARTUP_LOG%" 2>&1
    if errorlevel 1 (
        set "FAIL_REASON=Failed to create the Python virtual environment"
        goto :fatal
    )
)
venv\Scripts\python.exe -c "import sys" >nul 2>&1
if errorlevel 1 (
    set "FAIL_REASON=The existing Python virtual environment is invalid or belongs to another computer"
    goto :fatal
)
echo    OK
call :log "Python virtual environment is healthy"

REM ============================================================
REM  2. Python dependencies
REM ============================================================
echo  [2/4] Python dependencies (first install may take ~3 min)...
REM Only install if requirements.txt changed (or first run)
set NEED_PIP=0
if not exist "venv\.requirements-installed" (
    set NEED_PIP=1
) else (
    powershell.exe -NoProfile -NonInteractive -Command ^
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
        set "FAIL_REASON=Python dependencies failed to install"
        goto :fatal
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
            if errorlevel 1 (
                echo    WARNING: CUDA PyTorch install failed; keeping the current environment.
                call :log "WARNING: CUDA PyTorch install failed"
            ) else (
                echo %date% %time%> "venv\.requirements-installed"
            )
        )
    )
)
echo    OK
call :log "Python dependencies are ready"

REM ============================================================
REM  3. Frontend dependencies
REM ============================================================
echo  [3/4] Frontend dependencies (first install may take ~2 min)...
set "NEED_NPM=0"
if not exist "frontend\node_modules\.bin\electron-vite.cmd" set "NEED_NPM=1"
if "%NEED_NPM%"=="0" (
    call npm --prefix frontend ls --depth=0 >nul 2>&1
    if errorlevel 1 set "NEED_NPM=1"
)
if "%NEED_NPM%"=="1" (
    echo    Installing...
    call :log "Installing frontend dependencies"
    call :install_frontend_dependencies
    if errorlevel 1 (
        set "FAIL_REASON=npm install failed"
        goto :fatal
    )
)

REM npm ls only validates package metadata. Electron can still be half-installed
REM when its postinstall binary download was skipped, interrupted, or quarantined.
call npm --prefix frontend ls --depth=0 >> "%STARTUP_LOG%" 2>&1
if errorlevel 1 (
    set "FAIL_REASON=Frontend dependencies are still incomplete after npm install"
    goto :fatal
)
call :verify_electron
if errorlevel 1 (
    echo    Electron binary is missing or unusable, repairing...
    call :log "Electron verification failed; rebuilding Electron"
    call :rebuild_electron
    if errorlevel 1 (
        set "FAIL_REASON=Electron repair failed; check network, npm settings, antivirus, and the startup log"
        goto :fatal
    )
    call :verify_electron
    if errorlevel 1 (
        set "FAIL_REASON=Electron is still unavailable after repair; check antivirus quarantine and the startup log"
        goto :fatal
    )
)
echo    OK
call :log "Frontend dependencies are ready"

REM ============================================================
REM  4. Start Electron (LauncherManager spawns backend internally)
REM ============================================================
echo  [4/4] Starting frontend...
echo    Electron launching...
call :log "Starting Electron frontend"
start "" /B cmd /d /c "call npm --prefix frontend run dev" >> "%STARTUP_LOG%" 2>&1
if errorlevel 1 (
    set "FAIL_REASON=Failed to create the Electron frontend process"
    goto :fatal
)

REM ============================================================
REM  Done
REM ============================================================
title Fengjin AI - Running
echo.
echo    ====================================
echo    Fengjin AI is running!
echo.
echo    Backend:  configured port ^(automatic fallback enabled^)
echo    Startup:  logs\startup.log
echo    App logs: logs\app.log
echo.
echo    Close this window to stop all services.
echo    ====================================
echo.
pause

REM Cleanup
echo.
echo    Shutting down...
call :stop_tracked_backend
call :log "Shutdown cleanup completed"
echo    Goodbye.
goto :eof

:stop_tracked_backend
powershell.exe -NoProfile -NonInteractive -Command ^
  "$pidPath = Join-Path (Get-Location) 'logs\backend.pid';" ^
  "if (-not (Test-Path -LiteralPath $pidPath)) { Write-Host '  No tracked backend'; exit 0 }" ^
  "$raw = (Get-Content -LiteralPath $pidPath -Raw).Trim(); $backendPid = 0;" ^
  "if (-not [int]::TryParse($raw, [ref]$backendPid)) { Remove-Item -LiteralPath $pidPath -Force; Write-Host '  Removed invalid backend PID file'; exit 0 }" ^
  "$proc = Get-Process -Id $backendPid -ErrorAction SilentlyContinue;" ^
  "if (-not $proc) { Remove-Item -LiteralPath $pidPath -Force; Write-Host '  Removed stale backend PID file'; exit 0 }" ^
  "if ($proc.Name -notmatch '^pythonw?$') { Remove-Item -LiteralPath $pidPath -Force; Write-Host '  WARNING: Tracked PID no longer matches Python; skipping'; exit 0 }" ^
  "try { $details = Get-CimInstance Win32_Process -Filter ('ProcessId = {0}' -f $backendPid) -OperationTimeoutSec 3 -ErrorAction Stop } catch { Write-Host '  WARNING: Timed out while verifying tracked backend; skipping'; exit 0 }" ^
  "if ($details.CommandLine -match '(^|\s)-m\s+src\.server\.server(\s|$)') {" ^
  "  try { Stop-Process -Id $backendPid -Force -ErrorAction Stop; Write-Host '  Stopped tracked backend (PID:' $backendPid ')' } catch { Write-Host '  WARNING: Unable to stop tracked backend (PID:' $backendPid ')' }" ^
  "} else { Write-Host '  WARNING: Tracked PID no longer matches this backend; skipping' }" ^
  "Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue"
exit /b

:log
>> "%STARTUP_LOG%" echo [%date% %time%] %~1
exit /b

:install_frontend_dependencies
powershell.exe -NoProfile -NonInteractive -Command ^
  "$logPath = $env:STARTUP_LOG;" ^
  "& npm.cmd --prefix frontend install --include=dev --ignore-scripts=false --registry=https://registry.npmmirror.com --foreground-scripts 2>&1 | Tee-Object -FilePath $logPath -Append;" ^
  "exit $LASTEXITCODE"
exit /b %errorlevel%

:verify_electron
if not exist "frontend\node_modules\.bin\electron.cmd" exit /b 1
call "frontend\node_modules\.bin\electron.cmd" --version >> "%STARTUP_LOG%" 2>&1
exit /b %errorlevel%

:rebuild_electron
powershell.exe -NoProfile -NonInteractive -Command ^
  "$logPath = $env:STARTUP_LOG;" ^
  "& npm.cmd --prefix frontend rebuild electron --ignore-scripts=false --foreground-scripts 2>&1 | Tee-Object -FilePath $logPath -Append;" ^
  "exit $LASTEXITCODE"
exit /b %errorlevel%

:fatal
echo.
echo    ERROR: %FAIL_REASON%
echo    Startup log: %STARTUP_LOG%
call :log "FATAL: %FAIL_REASON%"
echo.
pause
exit /b 1
