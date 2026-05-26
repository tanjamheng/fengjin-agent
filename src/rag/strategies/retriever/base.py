"""检索策略基类

定义所有检索策略的通用接口。
"""

from abc import ABC, abstractmethod
from typing import List
from pydantic import BaseModel


class SearchResult(BaseModel):
    """检索结果"""
    content: str
    score: float
    metadata: dict = {}
    source: str = ""


class RetrieverStrategy(ABC):
    """检索策略抽象基类"""

    @abstractmethod
    def retrieve(self, query: str) -> List[SearchResult]:
        """检索相关文档

        Args:
            query: 查询文本

        Returns:
            检索结果列表
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