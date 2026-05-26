"""无重排序策略

直接返回原始结果，不做任何排序调整。
"""

from typing import List
from .base import RerankerStrategy
from ..retriever.base import SearchResult


class NoneReranker(RerankerStrategy):
    """无重排序"""

    def __init__(self, **kwargs):
        # 忽略所有参数
        pass

    def initialize(self) -> None:
        """无需初始化"""
        pass

    def rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        """直接返回原始结果"""
        return results

    def cleanup(self) -> None:
        """无需清理"""
        pass