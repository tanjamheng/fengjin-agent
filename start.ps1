# Fengjin AI One-Click Startup (ASCII only - PS5.1 compatible)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

$Host.UI.RawUI.WindowTitle = "Fengjin AI - Starting..."

Write-Host ""
Write-Host "  =*= Fengjin AI - Cure the Twilight =*="
Write-Host "  ======================================"
Write-Host ""

$Python = "$Root\venv\Scripts\python.exe"

# ===== 1. Python venv =====
Write-Host "[1/4] Checking Python venv..." -ForegroundColor Cyan
if (-not (Test-Path $Python)) {
    Write-Host "  Creating venv..."
    python -m venv "$Root\venv"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to create venv. Python 3.10+ required." -ForegroundColor Red
        Read-Host "Press any key to exit"
        exit 1
    }
    Write-Host "  venv created."
}
Write-Host "  venv ready" -ForegroundColor Green

# ===== 2. pip deps =====
Write-Host "[2/4] Checking Python dependencies..." -ForegroundColor Cyan
& $Python -m pip install -r "$Root\requirements.txt" -q *>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [WARN] Some deps failed to install, continuing..." -ForegroundColor Yellow
}
Write-Host "  deps ready" -ForegroundColor Green

# ===== 3. frontend deps =====
Write-Host "[3/4] Checking frontend dependencies..." -ForegroundColor Cyan
if (-not (Test-Path "$Root\frontend\node_modules")) {
    Write-Host "  Installing frontend deps..."
    Push-Location "$Root\frontend"
    npm install
    Pop-Location
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to install frontend deps." -ForegroundColor Red
        Read-Host "Press any key to exit"
        exit 1
    }
}
Write-Host "  frontend deps ready" -ForegroundColor Green

# ===== 4. Launch =====
Write-Host "[4/4] Launching services..." -ForegroundColor Cyan
Write-Host ""

Write-Host "  -> Starting backend..."
# Kill any existing process on port 8765
$existing = netstat -ano 2>$null | Select-String "127.0.0.1:8765" | Select-String "LISTENING"
if ($existing) {
    Write-Host "  [WARN] Port 8765 occupied, killing old process..." -ForegroundColor Yellow
    $pidStr = ($existing -split '\s+')[-1]
    try { Stop-Process -Id ([int]$pidStr) -Force -ErrorAction Stop } catch {}
    Start-Sleep -Seconds 2
}

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "`$Host.UI.RawUI.WindowTitle = 'Fengjin AI - Backend'; cd '$Root'; & '$Python' -m src.server.server"
)

Write-Host "  -> Waiting for backend (5s)..."
Start-Sleep -Seconds 5

Write-Host "  -> Starting frontend..."
Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "`$Host.UI.RawUI.WindowTitle = 'Fengjin AI - Frontend'; cd '$Root\frontend'; npm run dev"
)

Write-Host ""
Write-Host "  ======================================"
Write-Host "  Launch complete!"
Write-Host ""
Write-Host "  Backend:  http://127.0.0.1:8765"
Write-Host "  Frontend: Electron window will open"
Write-Host ""
Write-Host "  Close this window anytime - services keep running."
Write-Host "  ======================================"
Write-Host ""
