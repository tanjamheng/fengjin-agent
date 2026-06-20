"""上下文恢复：进入旧会话时组装 LLM 上下文"""

from typing import Protocol, Optional

from .session import Session
from ..utils.logger import get_logger


class MemoryRetriever(Protocol):
    """记忆检索接口（和 context_manager.py 共用同一份定义）"""
    def retrieve(self, user_input: str) -> str: ...


class ContextRestorer:
    """上下文恢复器

    职责：
    1. 给用户展示最近 N 条消息（完整历史在 JSON 文件里）
    2. 给 LLM 组装有限上下文（复用 ContextManager.trim_messages()）
    """

    def __init__(self, context_manager=None, memory_retriever: Optional[MemoryRetriever] = None):
        self.context_manager = context_manager
        self.memory_retriever = memory_retriever
        self.log = get_logger("context_restorer")

    def restore_llm_context(self, session: Session) -> list[dict]:
        """组装 LLM 上下文，复用 ContextManager 的滑动窗口逻辑

        返回的消息列表和原会话最后一轮窗口里的内容一致。
        """
        messages = [{"role": msg.role, "content": msg.content} for msg in session.messages
                     if msg.role in ("user", "assistant")]

        if self.context_manager:
            messages = self.context_manager.trim_messages(messages)

        self.log.info("恢复上下文: {} 条消息", len(messages))
        return messages

    def restore_memory_context(self, session: Session) -> Optional[str]:
        """用最近几轮消息检索长期记忆，返回记忆文本"""
        if not self.memory_retriever:
            return None

        # 取最近 3 条用户消息作为检索 query
        user_msgs = [m.content for m in session.messages if m.role == "user"]
        if not user_msgs:
            return None

        query = " ".join(user_msgs[-3:])
        try:
            memory_text = self.memory_retriever.retrieve(query)
        except Exception as e:
            self.log.error("记忆检索失败（不阻塞会话恢复）: {}", e)
            return None
        if memory_text:
            self.log.info("检索到相关记忆")
        return memory_text
