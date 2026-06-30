"""ChromaDB 客户端注册表（进程级单例 + 引用计数）

DenseIndex 和 MemoryStorage 各自需要 ChromaDB PersistentClient。
为避免重复创建 SQLite 连接和 HNSW 索引元数据，此模块提供引用计数单例：
- acquire(path) → 首次创建，后续返回缓存实例
- release() → 引用计数-1，归零时关闭客户端
"""

import threading
from typing import Optional

import chromadb

from ..utils.logger import get_logger

log = get_logger("chroma_registry")

_lock = threading.Lock()
_client: Optional[chromadb.PersistentClient] = None
_path: Optional[str] = None
_refcount: int = 0


def acquire(path: str) -> chromadb.PersistentClient:
    """获取 ChromaDB 客户端实例（引用计数+1）

    首次调用时创建客户端；后续调用若路径相同则返回缓存实例。
    """
    global _client, _path, _refcount

    with _lock:
        if _client is not None:
            if path == _path:
                _refcount += 1
                log.info("复用 ChromaDB 客户端: {} (refcount={})", path, _refcount)
                return _client
            raise RuntimeError(
                f"ChromaDB 客户端路径冲突: 已有 {_path}, 请求 {path}"
            )

        # 首次创建
        log.info("创建 ChromaDB 客户端: {}", path)
        _client = chromadb.PersistentClient(
            path=path,
            settings=chromadb.Settings(anonymized_telemetry=False),
        )
        _path = path
        _refcount = 1
        return _client


def release() -> None:
    """释放引用（引用计数-1，归零时关闭客户端连接）"""
    global _client, _path, _refcount

    with _lock:
        if _refcount <= 0:
            log.warning("release() 调用次数超过 acquire()，refcount={}", _refcount)
            return
        _refcount -= 1
        if _refcount > 0:
            log.info("ChromaDB 客户端引用释放 (refcount={})", _refcount)
            return

        log.info("ChromaDB 客户端引用计数归零，关闭: {}", _path)
        if _client is not None:
            try:
                _client._system.stop()
            except Exception as e:
                log.warning("ChromaDB 客户端关闭异常: {}", e)
            _client = None
        _path = None
