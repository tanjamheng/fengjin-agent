"""分块策略入口"""

from .base import SplitterStrategy, TextChunk
from .fixed import FixedSplitter
from .recursive import RecursiveSplitter
from .semantic import SemanticSplitter
from .markdown import MarkdownSplitter

# 策略注册表
STRATEGY_MAP = {
    "fixed": FixedSplitter,
    "recursive": RecursiveSplitter,
    "semantic": SemanticSplitter,
    "markdown": MarkdownSplitter
}


def get_splitter(strategy_type: str, params: dict) -> SplitterStrategy:
    """获取分块策略实例

    Args:
        strategy_type: 策略类型 (fixed/recursive/semantic/markdown)
        params: 策略参数

    Returns:
        SplitterStrategy 实例
    """
    if strategy_type not in STRATEGY_MAP:
        raise ValueError(f"未知的分块策略: {strategy_type}")

    return STRATEGY_MAP[strategy_type](**params)


__all__ = [
    "SplitterStrategy",
    "TextChunk",
    "get_splitter",
    "FixedSplitter",
    "RecursiveSplitter",
    "SemanticSplitter",
    "MarkdownSplitter"
]