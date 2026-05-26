"""混合检索策略

结合稠密检索和稀疏检索，使用 RRF 融合结果。
"""

from typing import List
from .base import RetrieverStrategy, SearchResult


class HybridRetriever(RetrieverStrategy):
    """混合检索（调用 HybridIndex）"""

    def __init__(
        self,
        index,  # HybridIndex 实例
        top_k: int = 5,
        score_threshold: float = 0.5,
        rrf_k: int = 60
    ):
        """
        Args:
            index: HybridIndex 实例
            top_k: 返回数量
            score_threshold: RRF 分数阈值
            rrf_k: RRF 参数
        """
        self.index = index
        self.top_k = top_k
        self.score_threshold = score_threshold
        self.rrf_k = rrf_k

    def initialize(self) -> None:
        """初始化"""
        self.index.initialize()

    def retrieve(self, query: str) -> List[SearchResult]:
        """混合检索"""
        # HybridIndex 已经做了 RRF 融合
        raw_results = self.index.search(query, top_k=self.top_k)

        results = []
        for item in raw_results:
            score = item.get("rrf_score", 0)
            if score >= self.score_threshold:
                results.append(SearchResult(
                    content=item["content"],
                    score=score,
                    metadata=item.get("metadata", {}),
                    source=item.get("metadata", {}).get("file_name", "")
                ))

        return results

    def cleanup(self) -> None:
        """清理"""
        self.index.cleanup()