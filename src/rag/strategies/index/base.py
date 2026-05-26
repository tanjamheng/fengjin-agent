"""索引策略基类

定义所有索引策略的通用接口。
"""

from abc import ABC, abstractmethod
from typing import List, Any


class IndexStrategy(ABC):
    """索引策略抽象基类"""

    @abstractmethod
    def initialize(self) -> None:
        """初始化索引"""
        pass

    @abstractmethod
    def add(self, chunks: List[Any]) -> None:
        """添加文本块到索引

        Args:
            chunks: 文本块列表
        """
        pass

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> List[dict]:
        """搜索

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            搜索结果列表 [{"content": ..., "score": ..., "metadata": ...}]
        """
        pass

    @abstractmethod
    def count(self) -> int:
        """返回索引中的文档数量"""
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """清理资源"""
        pass