"""RAG MCP 服务器

将 RAGService 封装为 MCP 服务器，暴露 rag_retrieve 工具供 LLM 调用。
"""

from typing import Dict, Any, List
from ..capabilities.mcp_server import MCPServerBase
from ..rag.rag_service import RAGService
from ..utils.logger import get_logger


RAG_RETRIEVE_TOOL = {
    "type": "function",
    "function": {
        "name": "rag_retrieve",
        "description": (
            "从风堇的知识库中检索相关文档和设定资料。"
            "当用户的问题涉及翁法罗斯世界、风堇的角色设定、人物关系、剧情事件、台词风格、"
            "天空一族历史、泰坦、城邦、黄金裔等需要专业知识才能准确回答的内容时，调用此工具。"
            "对于日常闲聊（如问候、简单的情感交流、天气等），不需要调用此工具。"
            "返回内容以 [来源: 文档名] 标注每个匹配文档，多个文档用 --- 分隔线隔开；"
            "无匹配结果时返回引导性提示，请凭自身知识直接回答。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索查询，提取用户问题的核心内容作为查询词",
                }
            },
            "required": ["query"],
        },
    },
}


class RAGMCPServer(MCPServerBase):
    """RAG MCP 服务器

    暴露 rag_retrieve 工具，LLM 根据用户问题自行判断是否需要检索知识库。
    """

    def __init__(self, rag_service: RAGService):
        super().__init__(
            name="rag",
            description="风堇知识库检索服务，提供翁法罗斯世界设定、角色关系、剧情事件等知识检索"
        )
        self.rag_service = rag_service
        self.log = get_logger()

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [RAG_RETRIEVE_TOOL]

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        if tool_name == "rag_retrieve":
            query = arguments.get("query", "")
            if not query:
                return "错误：query 参数不能为空"

            context = self.rag_service.retrieve(query)
            if not context:
                return "未找到相关文档。请直接根据你的知识回答用户问题。"
            return context

        return f"未知工具: {tool_name}"

    def initialize(self) -> None:
        self.rag_service.initialize()
        self._initialized = True
        self.log.info("RAG MCP 服务器已初始化")

    def cleanup(self) -> None:
        self._initialized = False
        self.log.info("RAG MCP 服务器已清理")
