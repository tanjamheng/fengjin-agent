"""开发/生产启动入口

用法: python -m src.server.server
"""

import os
import sys
import yaml
from pathlib import Path

import uvicorn
from src.utils.logger import setup_logger, LogConfig


def main():
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

    config_path = Path(__file__).parent.parent.parent / "config" / "config.yaml"
    ws_config = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        ws_config = data.get("server", {})

    # Launcher 模式：uvicorn 日志也重定向到 loguru（文件），不污染 stdout
    _uvicorn_log_config = None
    if _is_launcher:
        _uvicorn_log_config = {
            "version": 1,
            "disable_existing_loggers": True,
            "handlers": {
                "null": {"class": "logging.NullHandler"},
            },
            "loggers": {
                "uvicorn": {"handlers": ["null"], "propagate": False},
                "uvicorn.error": {"handlers": ["null"], "propagate": False},
                "uvicorn.access": {"handlers": ["null"], "propagate": False},
            },
        }

    uvicorn.run(
        "src.server.app:create_app",
        host=ws_config.get("websocket_host", "127.0.0.1"),
        port=ws_config.get("websocket_port", 8765),
        factory=True,
        log_level="info" if not _is_launcher else "warning",
        log_config=_uvicorn_log_config,
    )


if __name__ == "__main__":
    main()
