"""Cross-Encoder 重排序策略

使用 CrossEncoder 模型对 (query, document) 打分并排序。
比 bi-encoder 更准确，但速度较慢。
"""

from typing import List
from pathlib import Path
from .base import RerankerStrategy
from ..retriever.base import SearchResult


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
            device: 推理设备 ("cpu" / "cuda")
        """
        self.model_name = model
        self.top_n = top_n
        self.device = device
        self._model = None

    def initialize(self) -> None:
        """加载模型"""
        try:
            import torch
            from sentence_transformers import CrossEncoder

            # 自动检测 GPU 可用性
            effective_device = self.device
            if self.device == "cuda" and not torch.cuda.is_available():
                effective_device = "cpu"

            model_path = self.model_name
            # 相对路径解析为项目根目录下的绝对路径
            if not Path(model_path).is_absolute():
                model_path = str(Path(__file__).parent.parent.parent.parent.parent / model_path)

            self._model = CrossEncoder(model_path, device=effective_device)
        except ImportError:
            raise ImportError("请安装 sentence-transformers: pip install sentence-transformers")

    def rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        """Cross-Encoder 重排序"""
        if not results or self._model is None:
            return results

        # 构建 (query, document) pairs
        pairs = [(query, result.content) for result in results]

        # 打分
        scores = self._model.predict(pairs)

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
        self._model = None