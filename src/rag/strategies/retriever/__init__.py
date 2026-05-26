"""检索策略入口"""

from .base import RetrieverStrategy, SearchResult
from .top_k import TopKRetriever
from .hybrid import HybridRetriever
from .parent_doc import ParentDocRetriever
from .hyde import HyDERetriever

# 策略注册表
STRATEGY_MAP = {
    "top_k": TopKRetriever,
    "hybrid": HybridRetriever,
    "parent_doc": ParentDocRetriever,
    "hyde": HyDERetriever
}


def get_retriever(strategy_type: str, params: dict, index: "IndexStrategy") -> RetrieverStrategy:
    """获取检索策略实例

    Args:
        strategy_type: 策略类型 (top_k/hybrid/parent_doc/hyde)
        params: 策略参数
        index: 索引策略实例

    Returns:
        RetrieverStrategy 实例
    """
    if strategy_type not in STRATEGY_MAP:
        raise ValueError(f"未知的检索策略: {strategy_type}")

    return STRATEGY_MAP[strategy_type](index=index, **params)


__all__ = [
    "RetrieverStrategy",
    "SearchResult",
    "get_retriever",
    "TopKRetriever",
    "HybridRetriever",
    "ParentDocRetriever",
    "HyDERetriever"
]