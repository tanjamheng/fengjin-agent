"""开发/生产启动入口

用法: python -m src.server.server
"""

import yaml
from pathlib import Path

import uvicorn
from src.utils.logger import setup_logger, LogConfig


def main():
    setup_logger(LogConfig(log_level="DEBUG"))

    config_path = Path(__file__).parent.parent.parent / "config" / "config.yaml"
    ws_config = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        ws_config = data.get("server", {})

    uvicorn.run(
        "src.server.app:create_app",
        host=ws_config.get("websocket_host", "127.0.0.1"),
        port=ws_config.get("websocket_port", 8765),
        factory=True,
        log_level="info",
    )


if __name__ == "__main__":
    main()
