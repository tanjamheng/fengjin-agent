#!/usr/bin/env bash
# =*= Fengjin AI - Cure the Twilight =*=
# macOS / Linux 一键启动脚本，等价于 start.bat
set -euo pipefail
cd "$(dirname "$0")"

echo
echo "    =*= Fengjin AI - Cure the Twilight =*="
echo

# ── 0. 关闭旧的运行实例 ──
echo " [0/4] Checking for running instances..."

# 0a. 杀后端（8765 端口）
if command -v lsof &>/dev/null; then
    EXISTING=$(lsof -ti :8765 2>/dev/null || true)
    if [ -n "$EXISTING" ]; then
        for pid in $EXISTING; do
            PNAME=$(ps -p "$pid" -o comm= 2>/dev/null || true)
            if echo "$PNAME" | grep -qi 'python'; then
                kill "$pid" 2>/dev/null || true
                echo "  Stopped old backend (PID: $pid)"
            else
                echo "  Port 8765 in use by $PNAME (not ours, skipping)"
            fi
        done
    else
        echo "  No existing backend"
    fi
else
    echo "  (lsof not found, skipping port check)"
fi

# 0b. 关前端窗口
pkill -f "electron.*fengjin\|electron.*风堇" 2>/dev/null && echo "  Closed frontend window" || echo "  No existing frontend"
echo

# ── 0+. 关代理（国内源直连更快，避免绕路）──
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY 2>/dev/null || true
export ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
export ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/

# ── 1. Python venv ──
echo " [1/4] Python virtual environment..."
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null && "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "  ERROR: Python 3.10+ required but not found"
    exit 1
fi

if [ ! -f "venv/bin/python" ]; then
    echo "  Creating venv with $PYTHON..."
    "$PYTHON" -m venv venv
    if [ $? -ne 0 ]; then
        echo "  ERROR: Failed to create venv"
        exit 1
    fi
fi
echo "  OK ($PYTHON)"

# ── 2. Python dependencies ──
echo " [2/4] Python dependencies (first install may take ~3 min)..."
venv/bin/python -m pip install -r requirements.txt -q >/dev/null 2>&1 || {
    echo "  WARNING: Some dependencies failed to install, continuing..."
}
echo "  OK"

# ── 3. Frontend dependencies ──
echo " [3/4] Frontend dependencies (first install may take ~2 min)..."
if [ ! -d "frontend/node_modules" ]; then
    echo "  Installing..."
    (cd frontend && npm install) || {
        echo "  ERROR: npm install failed"
        exit 1
    }
fi
echo "  OK"

# ── 4. Start Electron ──
echo " [4/4] Starting frontend..."
echo "  Electron launching..."
cd frontend
npm run dev >/dev/null 2>&1 &
ELECTRON_PID=$!
cd ..

cat << 'EOF'

    ====================================
    Fengjin AI is running!

    Backend:  http://127.0.0.1:8765
    Logs:     logs/app.log

    Close this terminal to stop all services.
    ====================================

EOF

# Wait for Electron to exit, then clean up
wait $ELECTRON_PID 2>/dev/null || true

echo
echo "    Shutting down..."
# Kill any remaining process on 8765
if command -v lsof &>/dev/null; then
    REMAINING=$(lsof -ti :8765 2>/dev/null || true)
    [ -n "$REMAINING" ] && kill "$REMAINING" 2>/dev/null || true
fi
echo "    Goodbye."
