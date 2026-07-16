"""心智系统：记忆、情绪与羁绊的统一协调层。"""

from typing import TYPE_CHECKING

from .config import MindConfig, MindSettings

if TYPE_CHECKING:
    from .manager import MindManager

__all__ = ["MindConfig", "MindSettings", "MindManager"]


def __getattr__(name: str):
    """延迟导出协调器，避免 memory → mind → manager → memory 循环导入。"""
    if name == "MindManager":
        from .manager import MindManager

        return MindManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
