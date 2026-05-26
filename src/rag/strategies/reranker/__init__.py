"""重排序策略入口"""

from .base import RerankerStrategy
from .none import NoneReranker
from .cross_encoder import CrossEncoderReranker
from .llm import LLMReranker

# 策略注册表
STRATEGY_MAP = {
    "none": NoneReranker,
    "cross_encoder": CrossEncoderReranker,
    "llm": LLMReranker
}


def get_reranker(strategy_type: str, params: dict) -> RerankerStrategy:
    """获取重排序策略实例

    Args:
        strategy_type: 策略类型 (none/cross_encoder/llm)
        params: 策略参数

    Returns:
        RerankerStrategy 实例
    """
    if strategy_type not in STRATEGY_MAP:
        raise ValueError(f"未知的重排序策略: {strategy_type}")

    return STRATEGY_MAP[strategy_type](**params)


__all__ = [
    "RerankerStrategy",
    "get_reranker",
    "NoneReranker",
    "CrossEncoderReranker",
    "LLMReranker"
]