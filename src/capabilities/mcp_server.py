"""MCP 服务器基类

MCP（Model Context Protocol）是标准化的工具协议。
MCP 服务器暴露一组 Tool，通过 MCPManager 注册到 Agent 的 ToolRegistry。
LLM 通过 function calling 调用 MCP Tool，与本地 Tool 的调用方式一致。
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List


class MCPServerBase(ABC):
    """MCP 服务器抽象基类"""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self._initialized = False

    @abstractmethod
    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """返回 MCP 服务器暴露的工具定义列表（Anthropic tool format）"""
        pass

    @abstractmethod
    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """调用 MCP 工具，返回结果文本"""
        pass

    @abstractmethod
    def initialize(self) -> None:
        """初始化 MCP 服务器"""
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """清理 MCP 服务器资源"""
        pass

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def __repr__(self) -> str:
        return f"MCPServer({self.name})"
