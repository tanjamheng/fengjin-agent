"""Skill 能力基类

Skill 是提示词模版，根据场景注入到对话上下文中。
LLM 不知道 Skill 的存在，由系统根据 trigger_conditions 决定何时注入。
"""

from abc import ABC, abstractmethod
from pydantic import BaseModel
from typing import Any, Optional, List


class SkillMeta(BaseModel):
    """Skill 元信息"""

    name: str
    description: str
    trigger_conditions: str = ""
    version: str = "1.0.0"
    template_name: str = ""


class SkillContext(BaseModel):
    """Skill 执行上下文"""

    trace_id: str
    user_input: str
    conversation_history: List[dict] = []
    config: dict = {}
    extra: dict = {}


class SkillResult(BaseModel):
    """Skill 执行结果"""

    success: bool
    data: Any = None
    message: str = ""
    error: Optional[str] = None


class SkillBase(ABC):
    """Skill 抽象基类（提示词模版）

    execute() 接收上下文，返回 SkillResult。
    如果 data 中包含 "prompt" 字段，该内容会被注入到用户消息中。
    """

    def __init__(self, meta: SkillMeta):
        self.meta = meta
        self._initialized = False

    @abstractmethod
    def execute(self, context: SkillContext) -> SkillResult:
        """执行 Skill，返回包含提示词的执行结果"""
        pass

    def initialize(self) -> None:
        """初始化 Skill（子类可选择性重写）"""
        self._initialized = True

    def cleanup(self) -> None:
        """释放资源（子类可选择性重写）"""
        self._initialized = False

    def is_initialized(self) -> bool:
        return self._initialized

    def __repr__(self) -> str:
        return f"Skill({self.meta.name} v{self.meta.version})"

    def __str__(self) -> str:
        return self.meta.name
