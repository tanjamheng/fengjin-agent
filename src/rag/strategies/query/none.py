"""无增强策略

直接返回原始查询，不做任何修改。
"""

from typing import Union, List
from .base import QueryEnhancerStrategy


class NoneEnhancer(QueryEnhancerStrategy):
    """无增强"""

    def __init__(self):
        pass

    def initialize(self) -> None:
        """无需初始化"""
        pass

    def enhance(self, query: str) -> Union[str, List[str]]:
        """返回原始查询"""
        return query

    def cleanup(self) -> None:
        """无需清理"""
        pass