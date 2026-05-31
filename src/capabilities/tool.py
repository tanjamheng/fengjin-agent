"""Tool 能力基类

Tool 是可被 LLM 调用的函数，通过 function calling API 交互。
LLM 看到 Tool 的定义（名称、描述、参数 schema），自行决定何时调用。
"""

from abc import ABC, abstractmethod
from pydantic import BaseModel
from typing import Dict, Any


class ToolMeta(BaseModel):
    """Tool 元信息"""

    name: str
    description: str
    input_schema: Dict[str, Any]


class ToolBase(ABC):
    """Tool 抽象基类"""

    def __init__(self, meta: ToolMeta):
        self.meta = meta

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """执行 Tool，返回结果文本"""
        pass

    def to_definition(self) -> Dict[str, Any]:
        """转换为 OpenAI API tool 定义格式"""
        return {
            "type": "function",
            "function": {
                "name": self.meta.name,
                "description": self.meta.description,
                "parameters": self.meta.input_schema,
            },
        }

    def __repr__(self) -> str:
        return f"Tool({self.meta.name})"
