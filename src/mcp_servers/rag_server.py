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
            "检索风堇的官方角色资料与翁法罗斯世界观知识。"
            "凡是回答角色设定、人物关系、剧情事件、官方台词、风堇或小伊卡的能力与经历、"
            "天空一族历史、泰坦、城邦、黄金裔等游戏事实，必须先调用此工具，"
            "不要仅凭模型记忆作答；当用户质疑、纠正或追问这些事实时也必须重新检索。"
            "只有问候、陪伴、情绪交流等不依赖游戏知识的日常闲聊才不调用。"
            "返回内容以 [来源: 文档名] 标注每个匹配文档，多个文档用 --- 分隔线隔开；"
            "无匹配结果时应坦诚不确定，不得编造具体设定或剧情细节。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "用于知识库检索的完整自然语言问题。保留角色名、地点名、事件名和关系词，"
                        "不要只传过短关键词。"
                    ),
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
        self.log = get_logger("rag_server")

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
