"""风堇情绪状态机 — PAD 三维连续模型 + EMA 平滑 + 非对称指数衰减"""

from .engine import MoodEngine

__all__ = ["MoodEngine"]
