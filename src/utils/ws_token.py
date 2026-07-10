"""本地 WebSocket token 管理。"""

import os
import secrets
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_WS_TOKEN_FILE = _PROJECT_ROOT / "data" / "ws-token"


def get_or_create_ws_token(log=None) -> str:
    """获取本地 WS token。优先环境变量，否则读/生成 data/ws-token。"""
    token = os.environ.get("FENGJIN_WS_TOKEN", "").strip()
    if token:
        return token

    try:
        if _WS_TOKEN_FILE.exists():
            existing = _WS_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if len(existing) >= 32:
                os.environ["FENGJIN_WS_TOKEN"] = existing
                return existing
    except Exception as e:
        if log:
            log.warning("读取 WS token 失败: {}", e)

    try:
        _WS_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(32)
        _WS_TOKEN_FILE.write_text(token, encoding="utf-8")
        os.environ["FENGJIN_WS_TOKEN"] = token
        return token
    except Exception as e:
        if log:
            log.warning("生成 WS token 失败: {}", e)
        return ""
