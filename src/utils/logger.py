"""日志系统模块

基于 loguru 实现结构化日志，支持：
- trace_id 追踪
- 分级日志（DEBUG/INFO/WARNING/ERROR）
- 敏感信息脱敏
- 日志轮转
"""

import sys
from loguru import logger
from typing import Optional
import uuid
from functools import wraps


def generate_trace_id() -> str:
    """生成唯一 trace_id"""
    return str(uuid.uuid4())[:8]


def sanitize_message(message: str) -> str:
    """脱敏敏感信息"""
    import re
    # 脱敏 API Key
    message = re.sub(r'sk-[a-zA-Z0-9]{20,}', 'sk-***REDACTED***', message)
    # 脱敏其他可能的密钥格式
    message = re.sub(r'api_key["\']?\s*[:=]\s*["\'][^"\']{10,}["\']', 'api_key=***REDACTED***', message)
    return message


class LogConfig:
    """日志配置"""

    def __init__(
        self,
        log_dir: str = "logs",
        log_level: str = "INFO",
        rotation_size: str = "10 MB",
        retention_days: str = "7 days",
        json_format: bool = False
    ):
        self.log_dir = log_dir
        self.log_level = log_level
        self.rotation_size = rotation_size
        self.retention_days = retention_days
        self.json_format = json_format


def setup_logger(config: Optional[LogConfig] = None) -> None:
    """配置日志系统"""
    if config is None:
        config = LogConfig()

    # 移除默认 handler
    logger.remove()

    # 控制台输出（简洁格式，分两个 handler 避免 trace_id 缺失时格式串 KeyError）
    # Handler 1：有 trace_id 的正常日志（INFO+）
    logger.add(
        sys.stderr,
        level=config.log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{extra[trace_id]}</cyan> | "
               "<level>{message}</level>",
        filter=lambda record: "trace_id" in record["extra"]
    )
    # Handler 2：无 trace_id 的 WARNING+ 日志（初始化错误等），确保关键告警可见
    logger.add(
        sys.stderr,
        level="WARNING",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>-</cyan> | "
               "<level>{message}</level>",
        filter=lambda record: "trace_id" not in record["extra"]
    )

    # 文件输出（详细格式，带轮转；分两个 handler 避免无 trace_id 时格式串 KeyError）
    # Handler 1：有 trace_id 的正常日志
    logger.add(
        f"{config.log_dir}/agent_{config.log_level.lower()}.log",
        level=config.log_level,
        format=log_format,
        rotation=config.rotation_size,
        retention=config.retention_days,
        encoding="utf-8",
        enqueue=True,  # 异步写入
        filter=lambda record: "trace_id" in record["extra"]
    )
    # Handler 2：无 trace_id 的 WARNING+ 日志（初始化阶段的关键告警）
    logger.add(
        f"{config.log_dir}/agent_{config.log_level.lower()}.log",
        level="WARNING",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | "
               "{level: <8} | "
               "- | "
               "{name}:{function}:{line} | "
               "{message}",
        rotation=config.rotation_size,
        retention=config.retention_days,
        encoding="utf-8",
        enqueue=True,
        filter=lambda record: "trace_id" not in record["extra"]
    )

    # JSON 格式日志（用于分析）
    if config.json_format:
        logger.add(
            f"{config.log_dir}/agent_json.log",
            level=config.log_level,
            serialize=True,  # JSON 格式
            rotation=config.rotation_size,
            retention=config.retention_days,
            encoding="utf-8",
            enqueue=True
        )


def get_logger(trace_id: Optional[str] = None) -> "logger":
    """获取带 trace_id 的 logger"""
    if trace_id is None:
        trace_id = generate_trace_id()
    return logger.bind(trace_id=trace_id)


def with_trace_id(func):
    """装饰器：为函数自动添加 trace_id"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        trace_id = generate_trace_id()
        log = get_logger(trace_id)
        log.info("开始执行: {}", func.__name__)
        try:
            result = func(*args, **kwargs, _log=log, _trace_id=trace_id)
            log.info("完成执行: {}", func.__name__)
            return result
        except Exception as e:
            log.error("执行失败: {}, 错误: {}", func.__name__, sanitize_message(str(e)))
            raise
    return wrapper