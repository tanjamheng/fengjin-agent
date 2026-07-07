"""日志系统模块

基于 loguru 实现结构化日志，支持：
- trace_id 追踪（每次对话生成，贯穿全链路）
- source 模块标识（必传，可读模块名）
- component 自动分类（server / agent）
- 分级日志（DEBUG/INFO/WARNING/ERROR）
- 敏感信息脱敏
- 日志轮转（10MB / 7天）

source 与 trace_id 的语义约定（⚠️ 违反即日志不可追踪）：
- source: 模块标识字符串（如 "ws", "streaming", "core"）——调用方必传，不可用 uuid 替代
- trace_id: 请求追踪 ID（8位hex）——每次对话/请求重新生成，贯穿全链路
- 非请求事件（模块初始化等）：trace_id 自动填充 "--------"，表示无请求上下文
"""

import sys
from loguru import logger
from typing import Optional
import uuid
from functools import wraps


# ── 组件分类映射 ──────────────────────────────────────────
# source 名称 → component（server 或 agent）
# 不在 AGENT_SOURCES 中的 source 默认归为 "server"

AGENT_SOURCES = {
    # Agent 核心
    "core", "streaming", "context", "message_builder",
    "skill_registry", "tool_registry", "mcp_manager",
    "mood",
    "persona",
    # RAG
    "rag_service", "rag_server", "embedding_registry",
    "rag_loader", "rag_splitter", "rag_indexer", "rag_retriever",
    "rag_query_enhancer", "rag_reranker",
    # RAG 策略
    "hyde_retriever", "hyde", "cross_encoder_reranker", "llm_reranker",
    "dense_index", "query_decompose", "query_expand", "query_rewrite",
    "semantic_splitter",
}

# trace_id 占位符（非请求事件，8 位对齐 uuid 切片）
_NO_TRACE = "--------"


def _classify(source: str) -> str:
    """source 名称 → 组件分类"""
    return "agent" if source in AGENT_SOURCES else "server"


# ── 公开 API ──────────────────────────────────────────────


def generate_trace_id() -> str:
    """生成唯一 trace_id（8 位 hex）"""
    return str(uuid.uuid4())[:8]


def sanitize_message(message: str) -> str:
    """脱敏敏感信息"""
    import re
    message = re.sub(r'sk-[a-zA-Z0-9]{20,}', 'sk-***REDACTED***', message)
    message = re.sub(
        r'api_key["\']?\s*[:=]\s*["\'][^"\']{10,}["\']',
        'api_key=***REDACTED***', message,
    )
    return message


class LogConfig:
    """日志配置"""

    def __init__(
        self,
        log_dir: str = "logs",
        log_level: str = "DEBUG",
        rotation_size: str = "10 MB",
        retention_days: str = "7 days",
        json_format: bool = False,
    ):
        self.log_dir = log_dir
        self.log_level = log_level
        self.rotation_size = rotation_size
        self.retention_days = retention_days
        self.json_format = json_format


def setup_logger(config: Optional[LogConfig] = None) -> None:
    """配置日志系统 — 控制台 + app.log 文件输出"""
    if config is None:
        config = LogConfig()

    logger.remove()

    # ═══ 控制台输出 ═══
    # 简洁格式：时间 | 级别 | source | trace_id | 消息
    logger.add(
        sys.stderr,
        level=config.log_level,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "{extra[source]: <18} | "
            "<cyan>{extra[trace_id]}</cyan> | "
            "<level>{message}</level>"
        ),
    )

    # ═══ 文件输出 ═══
    # 详细格式：时间 | 级别 | source | trace_id | component | 位置 | 消息
    file_fmt = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <8} | "
        "{extra[source]: <18} | "
        "{extra[trace_id]: <8} | "
        "{extra[component]: <6} | "
        "{name}:{function}:{line} | "
        "{message}"
    )

    logger.add(
        f"{config.log_dir}/app.log",
        level=config.log_level,
        format=file_fmt,
        rotation=config.rotation_size,
        retention=config.retention_days,
        encoding="utf-8",
        enqueue=True,
    )

    # JSON 格式日志（全量，用于分析）
    if config.json_format:
        logger.add(
            f"{config.log_dir}/app_json.log",
            level=config.log_level,
            serialize=True,
            rotation=config.rotation_size,
            retention=config.retention_days,
            encoding="utf-8",
            enqueue=True,
        )


def get_logger(source: str, trace_id: Optional[str] = None) -> "logger":
    """获取绑定 source + component + trace_id 的 logger

    Args:
        source: 模块标识（如 "ws", "streaming", "core"）。
                必传。必须是可读字符串，禁止传 uuid。
        trace_id: 请求追踪 ID。None 时自动填充占位符（非请求事件）。

    Returns:
        绑定了 source / component / trace_id 的 loguru logger

    Examples:
        # 模块级 logger（无请求上下文）
        log = get_logger("ws")

        # 请求级 logger（绑定 trace_id）
        log = get_logger("core", trace_id="a1b2c3d4")
    """
    component = _classify(source)
    tid = trace_id or _NO_TRACE
    return logger.bind(source=source, component=component, trace_id=tid)


def with_trace_id(func):
    """装饰器：为函数自动添加 source + trace_id"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        trace_id = generate_trace_id()
        source = func.__qualname__.split(".")[0] if "." in func.__qualname__ else func.__name__
        log = get_logger(source, trace_id=trace_id)
        log.info("开始执行: {}", func.__name__)
        try:
            result = func(*args, **kwargs, _log=log, _trace_id=trace_id)
            log.info("完成执行: {}", func.__name__)
            return result
        except Exception as e:
            log.opt(exception=True).error(
                "执行失败: {}, 错误: {}", func.__name__, sanitize_message(str(e))
            )
            raise
    return wrapper
