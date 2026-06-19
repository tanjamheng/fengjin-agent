"""流式对话编排 — service 层

职责：安全检测 → 记忆合并 → 上下文组装 → LLM 流式生成 → yield token → 落盘
不含任何传输层细节（WebSocket / CLI 报文由调用方处理），可被多入口复用。

调用约定：
    async for token in stream_reply(...):
        # 调用方决定怎么展示 token（WS 发报文 / CLI 打印）
    # 生成器正常结束（含协作式取消）时，已 append assistant 消息 + flush
    # 被 task.cancel() 强制中断时抛 CancelledError，调用方需自行保存 partial_text
"""

from typing import AsyncGenerator

from openai import AsyncOpenAI

from ..session import SessionManager
from ..safety import SafetyManager, Action
from ..config import Config
from .context_manager import ContextManager
from .stream_controller import StreamController


class BlockedError(Exception):
    """用户消息被安全系统拦截，调用方需向用户展示拦截话术"""

    def __init__(self, message: str, category: str):
        super().__init__(message)
        self.message = message
        self.category = category


async def stream_reply(
    user_content: str,
    session_mgr: SessionManager,
    safety: SafetyManager,
    controller: StreamController,
    client: AsyncOpenAI,
    config: Config,
    context_mgr: ContextManager,
) -> AsyncGenerator[str, None]:
    """流式对话主链路，yield 每个 token

    流程：
    1. 安全检测 → 拦截则抛 BlockedError（调用方展示拦截话术，不入历史）
    2. 记忆合并 + 用户消息入历史
    3. 组装上下文（system + history + 记忆增强输入）+ 滑动窗口裁剪
    4. AsyncOpenAI 流式调用，逐 token yield（每 token 检查取消标志）
    5. 正常完成 / 协作式取消后：append assistant 消息 + flush
    """
    # 1. 安全检测
    result = safety.check(user_content)
    if result.action != Action.PASS:
        raise BlockedError(
            result.user_message or _default_blocked_message(result),
            result.category,
        )

    # 2. 记忆合并（复用 ContextManager）+ 用户消息入历史
    api_input = context_mgr.build_input(user_content)
    session_mgr.append_message("user", user_content)

    # 3. 组装上下文 + 滑动窗口
    api_messages = _build_api_messages(config, session_mgr, api_input)
    api_messages = context_mgr.trim_messages(api_messages)

    # 4. 流式调用 LLM
    stream = await client.chat.completions.create(
        model=config.model,
        messages=api_messages,
        temperature=config.agent.temperature,
        max_tokens=config.agent.max_tokens,
        stream=True,
    )

    full_text = ""
    try:
        async for chunk in stream:
            if controller.cancel_requested:
                break  # 协作式取消：break 后 finally 自动 response.aclose()

            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                controller.add_token(delta.content)
                full_text += delta.content
                yield delta.content
    finally:
        # 确保 stream 被关闭（正常结束 / 协作取消 / 异常都触发）
        try:
            await stream.close()
        except Exception:
            pass

    # 5. 落盘（正常完成或协作式取消都会走到这里；被 task.cancel 强制中断则不走到）
    if full_text:
        session_mgr.append_message("assistant", full_text)
        session_mgr.flush()


def _build_api_messages(
    config: Config,
    session_mgr: SessionManager,
    current_input: str,
) -> list[dict]:
    """组装 API messages：system_prompt + 历史消息（最后一条 user 换成记忆增强版）"""
    messages = [{"role": "system", "content": config.system_prompt}]
    messages.extend(session_mgr.get_current_messages())
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "user":
            messages[i]["content"] = current_input
            break
    return messages


def _default_blocked_message(result) -> str:
    """SafetyResult 无 user_message 时的兜底话术"""
    if result.action == Action.COMFORT:
        return "小伊卡提醒：风堇感觉到你有点不开心…先休息一下吧。"
    return "小伊卡提醒：风堇不想聊这个话题哦～换个话题吧？"
