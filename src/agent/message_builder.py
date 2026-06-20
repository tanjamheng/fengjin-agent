"""共享消息构建工具 — CLI/WS 两条路径复用

提供：
- assemble_system_prompt(): 组装 system prompt（含 comfort 安抚指令注入）
- rollback_last_user(): 回滚最后一条 user 消息，保持 user/assistant 配对
"""

from typing import Optional

from ..config import Config
from ..session import SessionManager
from ..utils.logger import get_logger

log = get_logger("message_builder")


def assemble_system_prompt(config: Config, comfort_prompt: Optional[str] = None) -> str:
    """组装 system prompt：基础人设 + 可选安抚指令（Comfort 模式）"""
    if comfort_prompt:
        return f"{config.system_prompt}\n\n{comfort_prompt}"
    return config.system_prompt


def rollback_last_user(
    session_mgr: "SessionManager",
    user_content: str,
    agent_messages: Optional[list] = None,
    msg_count_before: Optional[int] = None,
    session_count_before: Optional[int] = None,
) -> None:
    """回滚本轮对话：移除 session 和 agent 中本轮新增的消息

    - Session 侧：若提供 session_count_before，使用精确计数截断（CLI 路径）；
      否则检查末条消息是否匹配（WS 路径，user 始终是最后一条）。
    - Agent 侧：若提供 agent_messages 和 msg_count_before，索引截断到 msg_count_before。
    """
    session = session_mgr.current_session
    if session and session.messages:
        if session_count_before is not None:
            # CLI 路径：精确计数截断（最安全）
            while len(session.messages) > session_count_before:
                session.messages.pop()
        elif (session.messages
              and session.messages[-1].role == "user"
              and session.messages[-1].content == user_content):
            # WS 路径：末条位置检查（user 消息总是本轮最后一条）
            session.messages.pop()
            log.warning("已回滚本轮 user 消息以保持消息成对")

    # Agent 消息回滚：索引截断
    if agent_messages is not None and msg_count_before is not None:
        del agent_messages[msg_count_before:]
