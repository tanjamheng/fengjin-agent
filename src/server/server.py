"""开发/生产启动入口

用法: python -m src.server.server
"""

import os
import socket
import sys
import yaml
from pathlib import Path

import uvicorn
from src.utils.logger import setup_logger, LogConfig, get_logger
from src.utils.progress import emit_fatal


def _check_listen_endpoint(host: str, port: int) -> str | None:
    """在加载任何模型前确认 Uvicorn 目标端口可以绑定。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError as error:
        detail = (
            f"本地端口 {port} 无法使用（WinError {error.winerror or error.errno}）。"
            "可能被其他程序占用或被 Windows 系统保留；请关闭占用程序或重启电脑后重试。"
        )
        return "".join(detail)
    return None


def main() -> int:
    # ── Launcher 模式检测 ──
    _is_launcher = os.environ.get("FENGJIN_LAUNCHER_MODE") == "1"

    if _is_launcher:
        # stdout 专用于进度 JSON，所有日志只写文件
        setup_logger(LogConfig(
            log_level="DEBUG",
            json_format=False,
            stdout_enabled=False,       # 不写 stdout
            file_enabled=True,          # 只写 logs/app.log
        ))
    else:
        setup_logger(LogConfig(log_level="DEBUG"))
    log = get_logger("server")

    config_path = Path(__file__).parent.parent.parent / "config" / "config.yaml"
    ws_config = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        ws_config = data.get("server", {})

    host = ws_config.get("websocket_host", "127.0.0.1")
    port = int(ws_config.get("websocket_port", 8765))
    port_error = _check_listen_endpoint(host, port)
    if port_error:
        log.error("启动前端口预检失败: {}", port_error)
        emit_fatal("port_unavailable", port_error)
        return 1

    # Launcher 模式：uvicorn 日志也重定向到 loguru（文件），不污染 stdout
    _uvicorn_log_config = None
    if _is_launcher:
        _uvicorn_log_path = Path(__file__).parent.parent.parent / "logs" / "uvicorn.log"
        _uvicorn_log_path.parent.mkdir(parents=True, exist_ok=True)
        _uvicorn_log_config = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "()": "uvicorn.logging.DefaultFormatter",
                    "fmt": "%(asctime)s | %(levelprefix)s %(message)s",
                    "use_colors": False,
                },
            },
            "handlers": {
                "file": {
                    "class": "logging.FileHandler",
                    "filename": str(_uvicorn_log_path),
                    "mode": "a",
                    "encoding": "utf-8",
                    "formatter": "default",
                },
            },
            "loggers": {
                "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
                "uvicorn.error": {"handlers": ["file"], "level": "INFO", "propagate": False},
                "uvicorn.access": {"handlers": ["file"], "level": "WARNING", "propagate": False},
            },
        }

    uvicorn.run(
        "src.server.app:create_app",
        host=host,
        port=port,
        factory=True,
        log_level="info" if not _is_launcher else "warning",
        log_config=_uvicorn_log_config,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
