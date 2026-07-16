"""把会话历史整理为心智模型需要的自然对话轮次。"""

from typing import Iterable

from ..agent.context_manager import estimate_messages_tokens

_BLOCKED_PREFIX = "[小伊卡拦截]"


def normalize_turns(messages: Iterable[dict], *, max_turns: int, max_tokens: int) -> list[dict]:
    """按 user 边界组轮，只保留用户消息和最终自然语言 assistant 回复。"""
    turns: list[dict] = []
    current_user: str | None = None
    final_assistant: str | None = None

    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if not isinstance(content, str):
            continue
        if role == "user":
            if current_user is not None and final_assistant is not None:
                turns.append({"user": current_user, "assistant": final_assistant})
            current_user = content
            final_assistant = None
        elif role == "assistant" and current_user is not None:
            if message.get("tool_calls"):
                continue
            if content.startswith(_BLOCKED_PREFIX):
                current_user = None
                final_assistant = None
                continue
            final_assistant = content
        # system/tool/function 及所有工具中间消息均忽略

    if current_user is not None and final_assistant is not None:
        turns.append({"user": current_user, "assistant": final_assistant})

    turns = turns[-max_turns:]
    while len(turns) > 1 and _turn_tokens(turns) > max_tokens:
        turns.pop(0)

    if turns and _turn_tokens(turns) > max_tokens:
        turns[-1] = _truncate_latest_turn(turns[-1], max_tokens)
    return turns


def format_turns(turns: list[dict]) -> str:
    blocks = []
    for index, turn in enumerate(turns, start=1):
        marker = "（最新一轮，分析主体）" if index == len(turns) else "（历史语境）"
        blocks.append(
            f"第{index}轮{marker}\n用户：{turn['user']}\n风堇：{turn['assistant']}"
        )
    return "\n\n".join(blocks)


def _turn_tokens(turns: list[dict]) -> int:
    messages = []
    for turn in turns:
        messages.extend([
            {"role": "user", "content": turn["user"]},
            {"role": "assistant", "content": turn["assistant"]},
        ])
    return estimate_messages_tokens(messages)


def _truncate_latest_turn(turn: dict, max_tokens: int) -> dict:
    user = turn["user"]
    assistant = turn["assistant"]
    while estimate_messages_tokens([
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ]) > max_tokens and (len(user) > 64 or len(assistant) > 64):
        if len(assistant) >= len(user) and len(assistant) > 64:
            assistant = assistant[len(assistant) // 8:]
        elif len(user) > 64:
            user = user[len(user) // 8:]
    return {"user": user, "assistant": assistant}
