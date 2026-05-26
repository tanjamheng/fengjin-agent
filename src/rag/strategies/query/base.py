"""查询增强策略基类

定义所有查询增强策略的通用接口。
"""

from abc import ABC, abstractmethod
from typing import List, Union


class QueryEnhancerStrategy(ABC):
    """查询增强策略抽象基类"""

    @abstractmethod
    def enhance(self, query: str) -> Union[str, List[str]]:
        """增强查询

        Args:
            query: 原始查询

        Returns:
            增强后的查询（单个或多个）
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