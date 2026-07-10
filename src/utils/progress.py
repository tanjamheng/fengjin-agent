"""启动进度发射器 — launcher 模式下输出 JSON 行到 stdout

用法：
    from src.utils.progress import emit_progress, emit_warn, emit_fatal, emit_ready

    emit_progress("model_download:bge-m3", status="start")
    emit_progress("model_download:bge-m3", status="done")
    emit_warn("model_download:bge-m3", "下载超时，将跳过")
    emit_fatal("chromadb_corrupted", "向量数据库损坏，请删除 data/chroma 后重试")
    emit_ready()

仅在 FENGJIN_LAUNCHER_MODE=1 时输出。非 launcher 模式下所有函数均为空操作。
"""

import json
import os
import sys
from typing import Optional


def _is_launcher_mode() -> bool:
    return os.environ.get("FENGJIN_LAUNCHER_MODE") == "1"


def emit_preprocess_plan(steps: list[str]) -> None:
    """发送预处理步骤清单。必须是后端第一条消息。"""
    if not _is_launcher_mode():
        return
    _write({"type": "preprocess_plan", "steps": steps})


def emit_progress(step: str, status: str, percent: Optional[int] = None) -> None:
    """发送步骤进度。step 如 'model_download:bge-m3'，status 为 'start'/'done'/'progress'。

    status='progress' 时必须提供 percent (0-99)，表示当前步骤的百分比。
    """
    if not _is_launcher_mode():
        return
    payload: dict = {"type": "progress", "step": step, "status": status}
    if percent is not None:
        payload["percent"] = percent
    _write(payload)


def emit_warn(step: str, error: str) -> None:
    """发送非致命警告。跳过当前步骤，继续后续。"""
    if not _is_launcher_mode():
        return
    _write({"type": "warn", "step": step, "error": error})


def emit_fatal(error: str, detail: str) -> None:
    """发送致命错误。停止一切。"""
    if not _is_launcher_mode():
        return
    _write({"type": "fatal", "error": error, "detail": detail})


def emit_ready() -> None:
    """全部就绪。"""
    if not _is_launcher_mode():
        return
    _write({"type": "ready"})


def _write(obj: dict) -> None:
    """写一行 JSON 到 stdout，立即 flush。"""
    try:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:
        # 进度发射失败不应阻塞启动——静默忽略
        pass
