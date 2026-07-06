"""风堇羁绊状态机 — 追踪风堇与灰宝的羁绊关系（4 维）

提供：
  - BondTracker: 核心状态机（change clamp + 接近度衰减 + 指数衰减 + JSON 持久化）
  - BondSettings: 配置数据类（从 config/bond.yaml 加载）
"""

from .tracker import BondTracker, BondSettings

__all__ = ["BondTracker", "BondSettings"]
