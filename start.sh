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

# 0a. 仅清理本项目启动器记录的后端，不按端口扫描或误杀其他服务。
stop_tracked_backend() {
    local pid_file="logs/backend.pid"
    [ -f "$pid_file" ] || { echo "  No tracked backend"; return; }
    local pid command
    pid=$(tr -d '[:space:]' < "$pid_file")
    if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
        rm -f "$pid_file"
        echo "  Removed invalid backend PID file"
        return
    fi
    command=$(ps -p "$pid" -o command= 2>/dev/null || true)
    if [[ "$command" == *" -m src.server.server"* ]]; then
        kill "$pid" 2>/dev/null || true
        echo "  Stopped tracked backend (PID: $pid)"
    elif [ -n "$command" ]; then
        echo "  WARNING: Tracked PID no longer matches this backend; skipping"
    else
        echo "  Removed stale backend PID file"
    fi
    rm -f "$pid_file"
}
stop_tracked_backend

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
NEED_PIP=0
if [ ! -f "venv/.requirements-installed" ]; then
    NEED_PIP=1
elif [ "requirements.txt" -nt "venv/.requirements-installed" ]; then
    NEED_PIP=1
fi
if [ "$NEED_PIP" = "1" ]; then
    # Install torch first (GPU or CPU) so requirements.txt won't overwrite it
    if nvidia-smi >/dev/null 2>&1; then
        echo "  Installing CUDA PyTorch..."
        venv/bin/python -m pip install torch==2.6.0+cu124 --index-url https://mirrors.nju.edu.cn/pytorch/whl/cu124 --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple --force-reinstall --progress-bar on || true
    else
        echo "  Installing CPU PyTorch..."
        venv/bin/python -m pip install torch==2.6.0 -i https://pypi.tuna.tsinghua.edu.cn/simple --progress-bar on || true
    fi
    # Install remaining dependencies (torch already installed → pip skips it)
    echo "  Installing Python dependencies..."
    venv/bin/python -m pip install -r requirements.txt || {
        echo "  ERROR: Python dependencies failed to install"
        exit 1
    }
    date > "venv/.requirements-installed"
else
    # Even if deps already installed, verify torch matches GPU
    if nvidia-smi >/dev/null 2>&1; then
        if ! venv/bin/python -c "import torch; exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
            echo "  NVIDIA GPU detected but CPU-only PyTorch found, switching to CUDA PyTorch..."
            venv/bin/python -m pip install torch==2.6.0+cu124 --index-url https://mirrors.nju.edu.cn/pytorch/whl/cu124 --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple --force-reinstall --progress-bar on || true
            date > "venv/.requirements-installed"
        fi
    fi
fi
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

    Backend:  configured port (automatic fallback enabled)
    Logs:     logs/app.log

    Close this terminal to stop all services.
    ====================================

EOF

# Wait for Electron to exit, then clean up
wait $ELECTRON_PID 2>/dev/null || true

echo
echo "    Shutting down..."
stop_tracked_backend
echo "    Goodbye."
