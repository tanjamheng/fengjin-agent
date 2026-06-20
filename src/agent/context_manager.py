"""多轮对话上下文管理器"""

from typing import Optional, List, Dict, Protocol

from ..config import ContextConfig
from ..utils.logger import get_logger


class MemoryRetriever(Protocol):
    """记忆检索接口（协议类型，用于类型检查）"""
    def retrieve(self, user_input: str) -> str: ...


class ContextManager:
    """上下文管理器

    两个核心职责：
    1. 记忆合并：检索记忆 → 合并到当前 user message
    2. 滑动窗口：控制 messages 列表不超过 token 预算
    """

    def __init__(self, config: ContextConfig, memory_retriever: Optional[MemoryRetriever] = None):
        self.config = config
        self.memory_retriever = memory_retriever
        self.log = get_logger("context")

    def build_input(self, user_input: str) -> str:
        """组装当前轮的 user message

        流程：检索记忆 → 合并模板 → 返回增强后的输入。
        记忆系统未启用或检索结果为空时，返回原始输入。
        """
        if not self.config.memory.enabled:
            return user_input

        if not self.memory_retriever:
            return user_input

        try:
            memory_text = self.memory_retriever.retrieve(user_input)
        except Exception as e:
            self.log.error("记忆检索失败（不阻塞对话）: {}", e)
            return user_input
        if not memory_text:
            return user_input

        # Python format() 的参数值是原样插入的，不会被二次解析大括号
        return self.config.memory.template.format(
            memory=memory_text,
            input=user_input
        )

    def trim_messages(self, messages: List[Dict]) -> List[Dict]:
        """滑动窗口：从头部淘汰旧消息

        双重保护：
        1. 轮数上限：消息条数超过 max_turns * 2 时淘汰
        2. token 上限：估算 token 超过 max_tokens 时淘汰
        """
        max_messages = self.config.sliding_window.max_turns * 2

        # 按轮数淘汰
        while len(messages) > max_messages:
            self._pop_turn(messages)

        # 按 token 淘汰（兜底）
        max_tokens = self.config.sliding_window.max_tokens
        while self._estimate_tokens(messages) > max_tokens and len(messages) >= 2:
            self._pop_turn(messages)

        return messages

    def _pop_turn(self, messages: List[Dict]) -> None:
        """从头部弹出一轮对话（2~N 条消息）

        一轮正常对话 = user + assistant（2 条）
        一轮有 tool calling = user + assistant(tool_calls) + tool + tool + ... + assistant（N 条）

        从头部开始删，直到遇到下一条独立的 user 消息为止。
        """
        if not messages:
            return

        # 跳过 system prompt（不在裁剪范围内）
        if messages[0].get("role") == "system":
            return

        # 第一条必须是 user（对话起点）；若状态异常（非 user），记录警告后弹出非 user 消息
        if messages[0].get("role") != "user":
            self.log.warning("_pop_turn: 首条消息非 user (role={})，消息序列可能已损坏", messages[0].get('role'))
        messages.pop(0)

        # 继续删，直到遇到下一条独立的 user 消息
        while messages:
            msg = messages[0]
            role = msg.get("role", "")
            content = msg.get("content", "")

            # 跳过 system prompt（不应被裁剪）
            if role == "system":
                return

            # content 是 list（兼容旧格式），继续删
            if isinstance(content, list):
                messages.pop(0)
                continue

            # role=assistant（可能是 tool_calls 或普通回复），继续删
            if role == "assistant":
                messages.pop(0)
                continue

            # role=tool（OpenAI tool 结果消息），继续删
            if role == "tool":
                messages.pop(0)
                continue

            # 遇到 role=user 且 content 是字符串 = 下一轮起点，停止
            break

    @staticmethod
    def _estimate_tokens(messages: List[Dict]) -> int:
        """粗略估算消息列表的 token 数（中文 1 字 ≈ 1.5 token）"""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += len(str(block.get("content", "")))
                        total += len(str(block.get("text", "")))
            # tool_calls 字段也计入估算
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        total += len(str(tc.get("function", {}).get("arguments", "")))
        return int(total * 1.5)
