"""共享消息构建工具 — CLI/WS 两条路径复用

提供：
- assemble_system_prompt(): 组装 system prompt（含 comfort 安抚指令注入）
- rollback_last_user(): 回滚最后一条 user 消息，保持 user/assistant 配对
- DEFAULT_BLOCKED_MESSAGE: BLOCK 拦截无 user_message 时的统一兜底话术
"""

from typing import Optional

from ..config import Config
from ..session import SessionManager
from ..utils.logger import get_logger

log = get_logger("message_builder")

# BLOCK 拦截消息前缀 — core.py 和 _build_api_messages 统一引用，防止分散定义不同步
BLOCKED_PREFIX = "[小伊卡拦截]"

# BLOCK 拦截时若 category 未定义 user_message 的统一兜底话术
DEFAULT_BLOCKED_MESSAGE = "小伊卡发现了一些不太对劲的内容呢~请换个话题和风堇姐姐聊天吧！"


def assemble_system_prompt(config: Config, comfort_prompt: Optional[str] = None) -> str:
    """组装 system prompt：基础人设 + 可选安抚指令（Comfort 模式）"""
    if comfort_prompt:
        return f"{config.system_prompt}\n\n{comfort_prompt}"
    return config.system_prompt


def rollback_last_user(
    session_mgr: "SessionManager",
    user_content: str,
    session_count_before: Optional[int] = None,
) -> None:
    """回滚本轮对话：移除 session 中本轮新增的消息

    - 若提供 session_count_before，使用精确计数截断。
    - 否则检查末条消息是否匹配（user 始终是最后一条）。
    """
    session = session_mgr.current_session
    if session and session.messages:
        if session_count_before is not None:
            # 精确计数截断（最安全）
            while len(session.messages) > session_count_before:
                session.messages.pop()
        elif (session.messages
              and session.messages[-1].role == "user"
              and session.messages[-1].content == user_content):
            # 末条位置检查（user 消息总是本轮最后一条）
            session.messages.pop()
            log.warning("已回滚本轮 user 消息以保持消息成对")
