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


def _select_listen_port(host: str, primary_port: int, fallback_ports: list[object]) -> tuple[int | None, list[str]]:
    """按配置顺序选择可监听端口；失败详情仅用于最终的可见诊断。"""
    errors: list[str] = []
    candidates = [primary_port, *fallback_ports]
    seen: set[int] = set()
    for candidate in candidates:
        try:
            port = int(candidate)
        except (TypeError, ValueError):
            errors.append(f"端口配置无效: {candidate!r}")
            continue
        if not 1 <= port <= 65535:
            errors.append(f"端口超出范围: {port}")
            continue
        if port in seen:
            continue
        seen.add(port)
        error = _check_listen_endpoint(host, port)
        if error is None:
            return port, errors
        errors.append(error)
    return None, errors


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
    primary_port = int(ws_config.get("websocket_port", 8765))
    fallback_ports = ws_config.get("websocket_fallback_ports", [])
    if not isinstance(fallback_ports, list):
        fallback_ports = []
        log.warning("websocket_fallback_ports 必须为列表，已忽略无效配置")

    port, port_errors = _select_listen_port(host, primary_port, fallback_ports)
    if port is None:
        detail = "；".join(port_errors) or "未找到可用端口"
        log.error("启动前端口预检失败: {}", detail)
        emit_fatal("port_unavailable", detail)
        return 1
    if port != primary_port:
        log.warning("主端口 {} 不可用，已使用备用端口 {}", primary_port, port)
    os.environ["FENGJIN_ACTIVE_WS_PORT"] = str(port)

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
