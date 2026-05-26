"""查询增强策略入口"""

from .base import QueryEnhancerStrategy
from .none import NoneEnhancer
from .rewrite import RewriteEnhancer
from .decompose import DecomposeEnhancer
from .expand import ExpandEnhancer

# 策略注册表
STRATEGY_MAP = {
    "none": NoneEnhancer,
    "rewrite": RewriteEnhancer,
    "decompose": DecomposeEnhancer,
    "expand": ExpandEnhancer
}


def get_query_enhancer(strategy_type: str, params: dict) -> QueryEnhancerStrategy:
    """获取查询增强策略实例

    Args:
        strategy_type: 策略类型 (none/rewrite/decompose/expand)
        params: 策略参数

    Returns:
        QueryEnhancerStrategy 实例
    """
    if strategy_type not in STRATEGY_MAP:
        raise ValueError(f"未知的查询增强策略: {strategy_type}")

    return STRATEGY_MAP[strategy_type](**params)


__all__ = [
    "QueryEnhancerStrategy",
    "get_query_enhancer",
    "NoneEnhancer",
    "RewriteEnhancer",
    "DecomposeEnhancer",
    "ExpandEnhancer"
]