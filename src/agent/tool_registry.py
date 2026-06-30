"""Tool 注册中心

管理本地 Tool 和 MCP Tool，统一提供给 LLM 的 tool 定义。
"""

from typing import Dict, Any, List, Optional, Tuple
from ..capabilities.tool import ToolBase
from ..capabilities.mcp_server import MCPServerBase
from ..utils.logger import get_logger, generate_trace_id


class ToolRegistry:
    """Tool 注册中心（管理本地 Tool + MCP Tool）"""

    def __init__(self):
        self._local_tools: Dict[str, ToolBase] = {}
        # tool_name -> (mcp_server, tool_definition)
        self._mcp_tools: Dict[str, Tuple[MCPServerBase, dict]] = {}
        self.log = get_logger("tool_registry")

    def register_tool(self, tool: ToolBase) -> None:
        """注册本地 Tool"""
        name = tool.meta.name
        if name in self._local_tools or name in self._mcp_tools:
            self.log.warning("Tool {} 已存在，将被覆盖", name)
        self._local_tools[name] = tool
        self.log.info("注册 Tool: {}", name)

    def register_mcp_server(self, server: MCPServerBase) -> None:
        """注册 MCP 服务器的所有工具"""
        tool_defs = server.get_tool_definitions()
        for tool_def in tool_defs:
            # 兼容 OpenAI 嵌套格式 {"function": {"name": ...}} 和扁平格式 {"name": ...}
            name = tool_def.get("function", {}).get("name") or tool_def.get("name", "")
            if name in self._local_tools or name in self._mcp_tools:
                self.log.warning("MCP Tool {} 已存在，将被覆盖", name)
            self._mcp_tools[name] = (server, tool_def)
            self.log.info("注册 MCP Tool: {} (来自 {})", name, server.name)

    def unregister_tool(self, name: str) -> bool:
        """注销 Tool"""
        if name in self._local_tools:
            del self._local_tools[name]
            return True
        if name in self._mcp_tools:
            del self._mcp_tools[name]
            return True
        return False

    def get_all_definitions(self) -> List[Dict[str, Any]]:
        """返回所有 tool 定义（OpenAI API 格式）"""
        definitions = []
        for tool in self._local_tools.values():
            definitions.append(tool.to_definition())
        for _, tool_def in self._mcp_tools.values():
            definitions.append(tool_def)
        return definitions

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """执行指定工具（自动路由到本地或 MCP）"""
        # 本地 Tool
        if name in self._local_tools:
            tool = self._local_tools[name]
            return tool.execute(**arguments)

        # MCP Tool
        if name in self._mcp_tools:
            server, _ = self._mcp_tools[name]
            return server.call_tool(name, arguments)

        raise ValueError(f"Tool 不存在: {name}")

    def list_tools(self) -> List[dict]:
        """列出所有工具"""
        tools = []
        for name, tool in self._local_tools.items():
            tools.append({
                "name": name,
                "type": "local",
                "description": tool.meta.description,
            })
        for name, (server, tool_def) in self._mcp_tools.items():
            tools.append({
                "name": name,
                "type": "mcp",
                "source": server.name,
                "description": tool_def.get("function", {}).get("description", ""),
            })
        return tools

    @property
    def count(self) -> int:
        return len(self._local_tools) + len(self._mcp_tools)

    def clear(self) -> None:
        self._local_tools.clear()
        self._mcp_tools.clear()
