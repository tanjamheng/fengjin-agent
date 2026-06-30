"""MCP 管理器

管理所有 MCP 服务器实例的生命周期和工具发现。
"""

from typing import Dict, List, Optional
from ..capabilities.mcp_server import MCPServerBase
from ..utils.logger import get_logger, generate_trace_id


class MCPManager:
    """MCP 管理器"""

    def __init__(self):
        self._servers: Dict[str, MCPServerBase] = {}
        self.log = get_logger("mcp_manager")

    def register(self, server: MCPServerBase) -> None:
        """注册并初始化 MCP 服务器"""
        name = server.name
        if name in self._servers:
            self.log.warning("MCP 服务器 {} 已存在，将被覆盖", name)

        server.initialize()
        self._servers[name] = server
        self.log.info("注册 MCP 服务器: {}", name)

    def unregister(self, name: str) -> bool:
        """注销并清理 MCP 服务器"""
        if name not in self._servers:
            return False

        server = self._servers.pop(name)
        server.cleanup()
        self.log.info("注销 MCP 服务器: {}", name)
        return True

    def get_server(self, name: str) -> Optional[MCPServerBase]:
        return self._servers.get(name)

    def get_all_tool_definitions(self) -> List[dict]:
        """返回所有 MCP 服务器的工具定义"""
        definitions = []
        for server in self._servers.values():
            if server.is_initialized:
                definitions.extend(server.get_tool_definitions())
        return definitions

    def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        """通过 MCP 服务器调用工具"""
        server = self._servers.get(server_name)
        if not server:
            raise ValueError(f"MCP 服务器不存在: {server_name}")
        return server.call_tool(tool_name, arguments)

    def list_servers(self) -> List[dict]:
        """列出所有 MCP 服务器"""
        return [
            {
                "name": s.name,
                "description": s.description,
                "initialized": s.is_initialized,
                "tool_count": len(s.get_tool_definitions()),
            }
            for s in self._servers.values()
        ]

    def cleanup_all(self) -> None:
        """清理所有 MCP 服务器"""
        for server in self._servers.values():
            try:
                server.cleanup()
            except Exception as e:
                self.log.error("清理 MCP 服务器 {} 失败: {}", server.name, e)
        self._servers.clear()
        self.log.info("所有 MCP 服务器已清理")

    @property
    def count(self) -> int:
        return len(self._servers)
