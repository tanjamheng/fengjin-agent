"""流式对话编排 — service 层

职责：安全检测 → 记忆合并 → 上下文组装 → LLM 流式生成 → yield token → 落盘
不含任何传输层细节（WebSocket / CLI 报文由调用方处理），可被多入口复用。

安全三态（核心3 红线10 / 核心1 §2.5）：
- BLOCK     → raise BlockedError（拦截，消息记录到会话但不送入 LLM）
- COMFORT   → 放行，comfort_prompt 注入 system_prompt（自杀自伤安抚，不拦截）
- PASS      → 正常

调用约定：
    async for token in stream_reply(...):
        # 调用方决定怎么展示 token（WS 发报文 / CLI 打印）
    # 生成器正常结束（含协作式取消）时，已 append assistant 消息 + flush
    # 协作式取消且无 token 产出时，回滚已入历史的 user 消息
    # 被 task.cancel() 强制中断时抛 CancelledError，调用方需自行保存 partial_text
"""

import asyncio
from typing import AsyncGenerator, Optional

from openai import AsyncOpenAI

from ..session import SessionManager
from ..safety import SafetyManager, Action
from ..config import Config
from .context_manager import ContextManager
from .stream_controller import StreamController
from ..utils.logger import get_logger

log = get_logger("streaming")


class BlockedError(Exception):
    """BLOCK 拦截（非 COMFORT），调用方向用户展示拦截话术，消息已入历史"""

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
    trace_id: str = "",
) -> AsyncGenerator[str, None]:
    """流式对话主链路，yield 每个 token"""
    logger = log.bind(trace_id=trace_id) if trace_id else log

    # 1. 用户消息先入历史（无论安全判定如何，见核心1 §2.5）
    session_mgr.append_message("user", user_content)

    try:
        # 2. 安全检测（三态分流）
        result = safety.check(user_content)
        if result.action == Action.BLOCK:
            # 记录拦截占位消息到会话再抛出，保持 user/assistant 成对
            session_mgr.append_message("assistant", f"[小伊卡拦截] {result.user_message or _default_blocked_message()}")
            session_mgr.flush()
            raise BlockedError(
                result.user_message or _default_blocked_message(),
                result.category,
            )
        # COMFORT（自杀自伤安抚）：放行，安抚指令注入 system_prompt（红线10）
        comfort_prompt = result.comfort_prompt if result.action == Action.COMFORT else None

        # 3. 记忆合并（复用 ContextManager）
        api_input = context_mgr.build_input(user_content)
    except BlockedError:
        raise  # BlockedError 已记录消息+flush，直接传播
    except Exception:
        # 安全检测或记忆检索异常：回滚已入历史的 user 消息
        _rollback_last_user(session_mgr, user_content, logger)
        raise

    full_text = ""
    was_cancelled = False
    try:
        # 4. 组装上下文 + 滑动窗口
        api_messages = _build_api_messages(config, session_mgr, api_input, comfort_prompt)
        api_messages = context_mgr.trim_messages(api_messages)

        # 5. 流式调用 LLM
        stream = await client.chat.completions.create(
            model=config.model,
            messages=api_messages,
            temperature=config.agent.temperature,
            max_tokens=config.agent.max_tokens,
            stream=True,
        )

        try:
            async for chunk in stream:
                if controller.cancel_requested:
                    was_cancelled = True
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
            except Exception as e:
                logger.error("stream 关闭异常: {}", e)

    except asyncio.CancelledError:
        # task.cancel() 强制中断：回滚已入历史的 user 消息，保持 user/assistant 成对
        _rollback_last_user(session_mgr, user_content, logger)
        raise
    except Exception:
        # LLM 侧失败：回滚已入历史的 user 消息，保持 user/assistant 成对
        # （避免下一轮出现连续 user 消息破坏上下文组装）
        _rollback_last_user(session_mgr, user_content, logger)
        raise

    # 6. 落盘（正常完成或协作式取消都会走到这里；被 task.cancel 强制中断则不走到）
    if full_text:
        session_mgr.append_message("assistant", full_text)
        session_mgr.flush()
    elif was_cancelled:
        # 协作式取消且无 token 产出：回滚已入历史的 user 消息，避免孤立 user
        _rollback_last_user(session_mgr, user_content, logger)
    else:
        # LLM 返回空回复（罕见但可能）：写入空 assistant 保持消息成对
        session_mgr.append_message("assistant", "")
        session_mgr.flush()


def _build_api_messages(
    config: Config,
    session_mgr: SessionManager,
    current_input: str,
    comfort_prompt: Optional[str] = None,
) -> list[dict]:
    """组装 API messages：system_prompt(+安抚指令) + 历史消息（最后一条 user 换成记忆增强版）"""
    system_content = config.system_prompt
    if comfort_prompt:
        system_content = f"{config.system_prompt}\n\n{comfort_prompt}"
    messages = [{"role": "system", "content": system_content}]
    # 过滤 tool 角色消息：WS 路径不做 tool calling，tool 消息缺少 tool_call_id 会导致 API 报错
    history = [m for m in session_mgr.get_current_messages() if m.get("role") != "tool"]
    messages.extend(history)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "user":
            messages[i]["content"] = current_input
            break
    return messages


def _rollback_last_user(session_mgr: SessionManager, user_content: str, logger) -> None:
    """LLM 失败时回滚刚入历史的 user 消息，保持消息成对"""
    session = session_mgr.current_session
    if not session or not session.messages:
        return
    last = session.messages[-1]
    if last.role == "user" and last.content == user_content:
        session.messages.pop()
        logger.warning("LLM 失败，已回滚本轮 user 消息以保持消息成对")


def _default_blocked_message() -> str:
    """BLOCK 拦截无 user_message 时的兜底话术（COMFORT 不走此路径）"""
    return "小伊卡提醒：风堇不想聊这个话题哦～换个话题吧？"
