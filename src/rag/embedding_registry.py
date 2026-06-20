"""嵌入模型注册表（进程级单例 + 引用计数）

DenseIndex 和 MemoryStorage 各自需要加载 bge-m3 嵌入模型 (~1.1GB)。
为避免重复加载浪费内存/显存，此模块提供引用计数单例：
- acquire(model_path, device) → 首次加载，后续返回缓存实例
- release() → 引用计数-1，归零时释放模型
"""

import threading
from typing import Optional

from ..utils.logger import get_logger

log = get_logger("embedding_registry")

_lock = threading.Lock()
_model: Optional["SentenceTransformer"] = None
_model_path: Optional[str] = None
_refcount: int = 0


def acquire(model_path: str, device: str = "cpu") -> "SentenceTransformer":
    """获取嵌入模型实例（引用计数+1）

    首次调用时加载模型；后续调用若路径和设备相同则返回缓存实例。
    若路径或设备不同，发出警告并返回独立新实例（不回退到共享模式）。
    """
    global _model, _model_path, _refcount

    with _lock:
        if _model is not None:
            if model_path == _model_path:
                _refcount += 1
                log.info("复用已加载的嵌入模型: {} (refcount={})", model_path, _refcount)
                return _model
            # 路径不同：创建独立实例（不覆写全局状态，调用方自行管理生命周期）
            log.warning(
                "嵌入模型路径不匹配（已有: {}, 请求: {}），创建独立实例（不参与引用计数）",
                _model_path, model_path,
            )
            from sentence_transformers import SentenceTransformer
            return SentenceTransformer(model_path, device=device)

        # 首次加载
        from sentence_transformers import SentenceTransformer
        log.info("加载嵌入模型: {} (device={})", model_path, device)
        _model = SentenceTransformer(model_path, device=device)
        _model_path = model_path
        _refcount = 1
        return _model


def release() -> None:
    """释放引用（引用计数-1，归零时释放模型和 GPU 显存）"""
    global _model, _model_path, _refcount

    with _lock:
        if _refcount <= 0:
            log.warning("release() 调用次数超过 acquire()，refcount 已为 {}", _refcount)
            return
        _refcount -= 1
        if _refcount > 0:
            log.info("嵌入模型引用释放 (refcount={})", _refcount)
            return
        # 引用计数归零：释放模型
        log.info("嵌入模型引用计数归零，释放: {}", _model_path)
        if _model is not None:
            del _model
            _model = None
        _model_path = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception as e:
            log.warning("CUDA缓存清理异常: {}", e)


def _get_model() -> Optional["SentenceTransformer"]:
    """（仅供 SharedEmbeddingFunction 内部使用）获取当前共享模型"""
    return _model


class SharedEmbeddingFunction:
    """ChromaDB EmbeddingFunction 协议 — 包装共享嵌入模型

    替换 MemoryStorage 中的 SentenceTransformerEmbeddingFunction，
    复用 EmbeddingRegistry 中的共享模型实例。
    """

    def __init__(self, model_path: str, device: str = "cpu"):
        self._model_path = model_path
        self._device = device
        # acquire() 在 __init__ 中调用，对应 release() 在 cleanup 中调用
        self._model = acquire(model_path, device)

    def __call__(self, input_texts: list[str]) -> list[list[float]]:
        """ChromaDB 调用入口：将文本列表转为嵌入向量列表"""
        if self._model is None:
            raise RuntimeError("嵌入模型已释放")
        embeddings = self._model.encode(input_texts, convert_to_numpy=True)
        return embeddings.tolist()

    def cleanup(self) -> None:
        """释放共享模型的引用"""
        release()
        self._model = None
