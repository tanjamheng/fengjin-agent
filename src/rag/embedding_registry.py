"""嵌入模型注册表（进程级单例 + 引用计数）

DenseIndex 和 MemoryStorage 各自需要加载 bge-m3 嵌入模型 (~1.1GB)。
为避免重复加载浪费内存/显存，此模块提供引用计数单例：
- acquire(model_path, device) → 首次加载，后续返回缓存实例
- release() → 引用计数-1，归零时释放模型
"""

import threading
from pathlib import Path
from typing import Optional

from ..utils.logger import get_logger

log = get_logger("embedding_registry")

_lock = threading.Lock()
_model: Optional["SentenceTransformer"] = None
_model_path: Optional[str] = None
_model_device: Optional[str] = None
_refcount: int = 0


def acquire_handle(model_path: str, device: str = "cpu") -> tuple["SentenceTransformer", bool]:
    """获取嵌入模型实例和共享标记（引用计数+1）

    首次调用时加载模型；后续调用按路径复用已加载的实例。
    同一模型在一个进程内必须只有一个实际设备，避免 CPU/GPU 双份模型绕过预算。
    """
    global _model, _model_path, _model_device, _refcount

    with _lock:
        if _model is not None:
            if model_path == _model_path:
                _refcount += 1
                if device != _model_device:
                    log.info(
                        "嵌入模型请求设备 {} 与已加载实际设备 {} 不同，复用现有实例: {} (refcount={})",
                        device, _model_device, model_path, _refcount,
                    )
                else:
                    log.info(
                        "复用已加载的嵌入模型: {} (device={}, refcount={})",
                        model_path, _model_device, _refcount,
                    )
                return _model, True
            # 路径不同：创建独立实例（不覆写全局状态，调用方自行管理生命周期）
            log.warning(
                "嵌入模型路径不匹配（已有: {}, 请求: {}），创建独立实例（不参与引用计数）",
                _model_path, model_path,
            )
            from sentence_transformers import SentenceTransformer
            return SentenceTransformer(
                model_path,
                device=device,
                tokenizer_kwargs={"fix_mistral_regex": False},
            ), False

        # 首次加载：确保模型是 FP16（应由 ensure_models 预处理，此处为防御性兜底）
        import torch
        from sentence_transformers import SentenceTransformer
        from ..utils.gpu_budget import is_memory_oom

        _model_dir = Path(model_path)
        _state_file = _model_dir / ".state"

        def _load_st(fp16: bool, dev: str):
            if fp16:
                return SentenceTransformer(
                    model_path, device=dev,
                    model_kwargs={"dtype": torch.float16},
                    tokenizer_kwargs={"fix_mistral_regex": False},
                )
            m = SentenceTransformer(
                model_path,
                device=dev,
                tokenizer_kwargs={"fix_mistral_regex": False},
            )
            m.half()
            m.save(model_path, safe_serialization=True)
            _state_file.write_text("fp16")
            return m

        _fp16_ready = _state_file.exists() and _state_file.read_text().strip() == "fp16"
        try:
            _model = _load_st(_fp16_ready, device)
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            log.warning("嵌入模型 GPU 加载 OOM，降级 CPU: {}", model_path)
            try:
                _model = _load_st(_fp16_ready, "cpu")
            except Exception as cpu_error:
                if not is_memory_oom(cpu_error):
                    raise
                log.error("嵌入模型 CPU 加载 OOM，知识检索和记忆功能不可用")
                return None, True
        except RuntimeError as e:
            if "out of memory" in str(e).lower():
                torch.cuda.empty_cache()
                log.warning("嵌入模型 GPU 加载 OOM (RuntimeError)，降级 CPU: {}", model_path)
                try:
                    _model = _load_st(_fp16_ready, "cpu")
                except Exception as cpu_error:
                    if not is_memory_oom(cpu_error):
                        raise
                    log.error("嵌入模型 CPU 加载 OOM，知识检索和记忆功能不可用")
                    return None, True
            else:
                raise
        _model_path = model_path
        try:
            _model_device = str(_model.device)
        except Exception:
            _model_device = device
        _refcount = 1
        log.info("嵌入模型加载完成: {} (device={})", model_path, _model_device)
        return _model, True


def acquire(model_path: str, device: str = "cpu") -> "SentenceTransformer":
    """获取嵌入模型实例（引用计数+1）。"""
    model, _is_shared = acquire_handle(model_path, device)
    return model


def release() -> None:
    """释放引用（引用计数-1，归零时释放模型和 GPU 显存）"""
    global _model, _model_path, _model_device, _refcount

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
        _model_device = None
        import torch
        torch.cuda.empty_cache()


def _get_model() -> Optional["SentenceTransformer"]:
    """（仅供 SharedEmbeddingFunction 内部使用）获取当前共享模型"""
    return _model


class SharedEmbeddingFunction:
    """ChromaDB EmbeddingFunction 协议 — 包装共享嵌入模型

    替换 MemoryStorage 中的 SentenceTransformerEmbeddingFunction，
    复用 EmbeddingRegistry 中的共享模型实例。

    实现 ChromaDB ≥1.0 EmbeddingFunction 协议要求的 name() 方法。
    """

    def __init__(self, model_path: str, device: str = "cpu"):
        self._model_path = model_path
        self._device = device
        # acquire() 在 __init__ 中调用，对应 release() 在 cleanup 中调用
        self._model, self._is_shared = acquire_handle(model_path, device)

    @staticmethod
    def name() -> str:
        """ChromaDB 1.x 要求：返回嵌入函数的唯一标识名"""
        return "shared_bge_m3"

    def __call__(self, input: list[str]) -> list[list[float]]:
        """ChromaDB 调用入口：将文本列表转为嵌入向量列表
        （参数名必须为 input，ChromaDB 1.x check_types 会校验签名）
        """
        if self._model is None:
            raise RuntimeError("嵌入模型已释放")
        import torch
        with torch.inference_mode():
            embeddings = self._model.encode(
                input, convert_to_numpy=True, batch_size=64,
                normalize_embeddings=True, show_progress_bar=False,
            )
        return embeddings.tolist()

    def cleanup(self) -> None:
        """释放模型引用；独立实例不触碰全局引用计数。"""
        if self._model is None:
            return
        if self._is_shared:
            release()
        self._model = None
        self._is_shared = False
