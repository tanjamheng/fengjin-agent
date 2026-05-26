"""重排序策略基类

定义所有重排序策略的通用接口。
"""

from abc import ABC, abstractmethod
from typing import List
from ..retriever.base import SearchResult


class RerankerStrategy(ABC):
    """重排序策略抽象基类"""

    @abstractmethod
    def rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        """重排序

        Args:
            query: 查询文本
            results: 待重排序的结果列表

        Returns:
            重排序后的结果列表
        """
        pass

    @abstractmethod
    def initialize(self) -> None:
        """初始化"""
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """清理资源"""
        pass