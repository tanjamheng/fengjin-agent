"""Cross-Encoder 重排序策略

使用 CrossEncoder 模型对 (query, document) 打分并排序。
比 bi-encoder 更准确，但速度较慢。
"""

from typing import List
from pathlib import Path
from .base import RerankerStrategy
from ..retriever.base import SearchResult
from ....utils.logger import get_logger


class CrossEncoderReranker(RerankerStrategy):
    """Cross-Encoder 重排序"""

    def __init__(
        self,
        model: str = "BAAI/bge-reranker-v2-m3",
        top_n: int = 3,
        device: str = "cpu"
    ):
        """
        Args:
            model: CrossEncoder 模型名称
            top_n: 返回数量
            device: 推理设备 ("cpu" / "cuda" / "auto")，auto 下自动检测 GPU 可用性降级
        """
        self.model_name = model
        self.top_n = top_n
        self.device = device
        self._model = None
        self.log = get_logger("cross_encoder_reranker")

    def initialize(self) -> None:
        """加载模型（FP16 精度：首次加载转换并覆盖磁盘，后续直读 FP16）"""
        try:
            import torch
            from sentence_transformers import CrossEncoder
            from ....utils.helpers import resolve_device

            # 自动检测 GPU 可用性
            effective_device = resolve_device(self.device)

            model_path = self.model_name
            # 相对路径解析为项目根目录下的绝对路径
            if not Path(model_path).is_absolute():
                from ....utils.helpers import get_project_root
                model_path = str(get_project_root() / model_path)

            _model_dir = Path(model_path)
            _state_file = _model_dir / ".state"
            if _state_file.exists() and _state_file.read_text().strip() == "fp16":
                self.log.info("加载重排序模型: {} (device={}, dtype=float16)", model_path, effective_device)
                self._model = CrossEncoder(
                    model_path,
                    device=effective_device,
                    automodel_args={"torch_dtype": torch.float16},
                )
            else:
                # 防御路径：ensure_models 未运行或中途崩溃，现场量化
                self.log.warning("重排序模型 {} 未预量化为 FP16，现场处理...", model_path)
                self._model = CrossEncoder(model_path, device=effective_device)
                self._model.model.half()
                self._model.model.save_pretrained(model_path, safe_serialization=True)
                if hasattr(self._model, "tokenizer") and self._model.tokenizer is not None:
                    self._model.tokenizer.save_pretrained(model_path)
                _state_file.write_text("fp16")
                self.log.info("FP16 重排序模型已保存至 {}", model_path)
        except ImportError:
            raise ImportError("请安装 sentence-transformers: pip install sentence-transformers")

    def rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        """Cross-Encoder 重排序"""
        if not results or self._model is None:
            return results

        # 构建 (query, document) pairs
        pairs = [(query, result.content) for result in results]

        # 打分
        import torch
        with torch.inference_mode():
            scores = self._model.predict(
                pairs,
                activation_fct=None,       # bge-reranker-v2-m3 输出 logits，不压缩
                batch_size=32,
                show_progress_bar=False,
            )

        # 按分数排序
        scored_results = []
        for i, result in enumerate(results):
            scored_results.append(SearchResult(
                content=result.content,
                score=float(scores[i]),
                metadata={**result.metadata, "rerank_method": "cross_encoder"},
                source=result.source
            ))

        # 排序
        scored_results.sort(key=lambda x: x.score, reverse=True)

        # 返回 top_n
        return scored_results[:self.top_n]

    def cleanup(self) -> None:
        """清理模型"""
        if self._model is not None:
            del self._model
            self._model = None
            import torch
            torch.cuda.empty_cache()