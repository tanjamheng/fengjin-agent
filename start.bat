@echo off
cd /d "%~dp0"

echo.
echo   =*= Fengjin AI - Cure the Twilight =*=
echo   ======================================
echo.

REM ==== 1. venv ====
echo [1/4] Checking Python venv...
if not exist "venv\Scripts\python.exe" (
    echo   Creating venv...
    python -m venv venv
    if errorlevel 1 (
        echo   ERROR: Failed to create venv. Python 3.10+ required.
        pause
        exit /b 1
    )
)
echo   venv ready.

REM ==== 2. pip deps ====
echo [2/4] Checking Python dependencies...
call venv\Scripts\python.exe -m pip install -r requirements.txt -q >nul 2>&1
if errorlevel 1 (
    echo   WARNING: Some deps failed to install, continuing...
)
echo   deps ready.

REM ==== 3. frontend deps ====
echo [3/4] Checking frontend dependencies...
if not exist "frontend\node_modules\" (
    echo   Installing frontend deps...
    cd frontend
    call npm install
    cd ..
    if errorlevel 1 (
        echo   ERROR: Failed to install frontend deps.
        pause
        exit /b 1
    )
)
echo   frontend deps ready.

REM ==== 4. launch ====
echo [4/4] Launching services...
echo.

echo   -^> Starting backend...
start "Fengjin AI - Backend" cmd /c "cd /d "%CD%" && venv\Scripts\python.exe -m src.server.server && pause"

echo   -^> Waiting for backend to be fully ready...
powershell -Command "for($i=0;$i -lt 120;$i++){try{$r=irm http://127.0.0.1:8765/health -TimeoutSec 2;if($r.status -eq 'ready'){exit 0}}catch{};Start-Sleep 1};exit 1"
if errorlevel 1 (
    echo   ERROR: Backend failed to start within 120s.
    pause
    exit /b 1
)
echo   Backend ready - all models loaded.

echo   -^> Starting frontend...
start "Fengjin AI - Frontend" cmd /c "cd /d "%CD%\frontend" && npm run dev && pause"

echo.
echo   ======================================
echo   Launch complete!
echo.
echo   Backend:  http://127.0.0.1:8765
echo   Frontend: Electron window will open
echo.
echo   Close this window anytime.
echo   ======================================
echo.
pause
