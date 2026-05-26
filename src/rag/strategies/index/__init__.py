"""索引策略入口"""

from .base import IndexStrategy
from .dense import DenseIndex
from .sparse import SparseIndex
from .hybrid import HybridIndex

# 策略注册表
STRATEGY_MAP = {
    "dense": DenseIndex,
    "sparse": SparseIndex,
    "hybrid": HybridIndex
}


def get_index(strategy_type: str, params: dict) -> IndexStrategy:
    """获取索引策略实例

    Args:
        strategy_type: 策略类型 (dense/sparse/hybrid)
        params: 策略参数

    Returns:
        IndexStrategy 实例
    """
    if strategy_type not in STRATEGY_MAP:
        raise ValueError(f"未知的索引策略: {strategy_type}")

    return STRATEGY_MAP[strategy_type](**params)


__all__ = [
    "IndexStrategy",
    "get_index",
    "DenseIndex",
    "SparseIndex",
    "HybridIndex"
]