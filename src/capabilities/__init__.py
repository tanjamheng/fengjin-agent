"""能力基类包

定义 Agent 的三种能力类型：
- Skill：提示词模版，系统决定注入时机，LLM 不感知
- Tool：带 JSON Schema 的函数，LLM 通过 function calling 自主调用
- MCP：标准化工具协议，MCP 服务器暴露 Tool，LLM 同样通过 function calling 调用
"""

from .skill import SkillBase, SkillMeta, SkillContext, SkillResult
from .tool import ToolBase, ToolMeta
from .mcp_server import MCPServerBase

__all__ = [
    "SkillBase", "SkillMeta", "SkillContext", "SkillResult",
    "ToolBase", "ToolMeta",
    "MCPServerBase",
]
