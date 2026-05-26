"""Top-K 向量检索策略

基础的向量检索，返回最相似的 K 个文档。
"""

from typing import List
from .base import RetrieverStrategy, SearchResult


class TopKRetriever(RetrieverStrategy):
    """Top-K 向量检索"""

    def __init__(
        self,
        index,
        top_k: int = 5,
        score_threshold: float = 0.7
    ):
        """
        Args:
            index: 索引策略实例
            top_k: 返回数量
            score_threshold: 最低分数阈值
        """
        self.index = index
        self.top_k = top_k
        self.score_threshold = score_threshold

    def initialize(self) -> None:
        """初始化索引"""
        self.index.initialize()

    def retrieve(self, query: str) -> List[SearchResult]:
        """检索"""
        raw_results = self.index.search(query, top_k=self.top_k)

        results = []
        for item in raw_results:
            score = self._convert_score(item.get("distance", 0), item.get("score", 0))
            if score >= self.score_threshold:
                results.append(SearchResult(
                    content=item["content"],
                    score=score,
                    metadata=item.get("metadata", {}),
                    source=item.get("metadata", {}).get("file_name", "")
                ))

        return results

    def _convert_score(self, distance: float, score: float) -> float:
        """将距离/分数转换为相似度

        Chroma 默认使用 L2 距离，高维 embedding 下 distance 值较大。
        使用归一化方式：距离越小，分数越高
        """
        # 如果已经有 score，直接返回
        if score > 0:
            return score

        # L2 距离转换：使用指数衰减
        # distance ~10 表示中等相似度，distance ~20 表示低相似度
        # 转换后：distance=10 → 0.37, distance=20 → 0.14
        import math
        return math.exp(-distance / 10.0)

    def cleanup(self) -> None:
        """清理"""
        self.index.cleanup()