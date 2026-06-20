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
import json
from typing import AsyncGenerator, Callable, Optional

from openai import AsyncOpenAI

from ..session import SessionManager
from ..safety import SafetyManager, Action
from ..config import Config
from .context_manager import ContextManager
from .stream_controller import StreamController
from .message_builder import assemble_system_prompt, rollback_last_user as _rollback
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
    session_mgr: "SessionManager",
    safety: "SafetyManager",
    controller: "StreamController",
    client: "AsyncOpenAI",
    config: "Config",
    context_mgr: "ContextManager",
    trace_id: str = "",
    tool_definitions: Optional[list] = None,
    execute_tool_async: Optional[Callable] = None,
) -> AsyncGenerator[str, None]:
    """流式对话主链路，yield 每个 token

    Args:
        tool_definitions: OpenAI 格式的 tool 定义列表（None=不启用 Tool Calling）
        execute_tool_async: async callable(name, arguments) -> str（tool_definitions 非空时必填）
    """
    logger = log.bind(trace_id=trace_id) if trace_id else log

    # 1. 用户消息先入历史（无论安全判定如何，见核心1 §2.5）
    session_mgr.append_message("user", user_content)

    try:
        # 2. 安全检测（三态分流）
        result = safety.check(user_content, trace_id=trace_id)
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
        api_input = context_mgr.build_input(user_content, trace_id=trace_id)
    except BlockedError:
        raise  # BlockedError 已记录消息+flush，直接传播
    except Exception:
        # 安全检测或记忆检索异常：回滚已入历史的 user 消息
        _rollback(session_mgr, user_content)
        raise

    full_text = ""
    was_cancelled = False
    # Tool calling 中间消息（仅用于 API 调用，不入 session）
    tool_loop_messages: list[dict] = []
    tool_rounds = 0
    max_tool_rounds = config.agent.max_tool_rounds if tool_definitions else 0

    try:
        while True:
            # 4. 组装上下文 + 滑动窗口（system 不在裁剪范围内，剥离后单独管理）
            system_content = assemble_system_prompt(config, comfort_prompt)
            api_messages = _build_api_messages(
                config, session_mgr, api_input, system_content, tool_loop_messages,
            )
            # 剥离 system 再裁剪（_pop_turn 遇 system 直接 return 导致死循环）
            system_msg = api_messages[0] if api_messages and api_messages[0].get("role") == "system" else None
            trim_target = api_messages[1:] if system_msg else api_messages
            trim_target = context_mgr.trim_messages(trim_target, trace_id=trace_id)
            api_messages = [system_msg] + trim_target if system_msg else trim_target

            # 5. 流式调用 LLM（含 tool_definitions）
            api_params = {
                "model": config.model,
                "messages": api_messages,
                "temperature": config.agent.temperature,
                "max_tokens": config.agent.max_tokens,
                "stream": True,
            }
            if tool_definitions:
                api_params["tools"] = tool_definitions

            stream = await client.chat.completions.create(**api_params)

            # 流式处理：累积文本 + tool_calls delta
            tool_calls_data: dict[int, dict] = {}  # index -> {id, name, arguments}

            try:
                async for chunk in stream:
                    if controller.cancel_requested:
                        was_cancelled = True
                        break  # 协作式取消

                    delta = chunk.choices[0].delta if chunk.choices else None
                    if not delta:
                        continue

                    # 文本 token → yield
                    if delta.content:
                        controller.add_token(delta.content)
                        full_text += delta.content
                        yield delta.content

                    # tool_calls 增量累积
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_calls_data:
                                tool_calls_data[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": "",
                                    "arguments": "",
                                }
                            if tc_delta.id:
                                tool_calls_data[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    tool_calls_data[idx]["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    tool_calls_data[idx]["arguments"] += tc_delta.function.arguments
            finally:
                try:
                    await stream.close()
                except Exception as e:
                    logger.error("stream 关闭异常: {}", e)

            if was_cancelled:
                break

            # 6. Tool calling：有 tool_calls 时执行工具并循环
            if not tool_calls_data or tool_rounds >= max_tool_rounds:
                break

            tool_rounds += 1
            logger.info("WS Tool calling 第 {} 轮，{} 个工具调用", tool_rounds, len(tool_calls_data))

            # 构建 assistant 消息（含 tool_calls）并加入循环消息
            tool_calls_serialized = _serialize_tool_calls(tool_calls_data)
            tool_loop_messages.append({
                "role": "assistant",
                "content": full_text or "",
                "tool_calls": tool_calls_serialized,
            })

            # 执行工具（同步 → asyncio.to_thread）
            for idx in sorted(tool_calls_data.keys()):
                tc = tool_calls_data[idx]
                tool_use_id = tc["id"]
                tool_name = tc["name"]
                try:
                    tool_input = json.loads(tc["arguments"])
                except json.JSONDecodeError:
                    logger.warning("WS Tool {} 参数 JSON 解析失败: {}", tool_name,
                                   tc["arguments"][:100])
                    tool_input = {}

                try:
                    if execute_tool_async is None:
                        raise RuntimeError("execute_tool_async 未提供")
                    result_text = await execute_tool_async(tool_name, tool_input)
                    logger.info("WS Tool {} 执行成功", tool_name)
                except Exception as e:
                    logger.error("WS Tool {} 执行失败: {}", tool_name, e)
                    result_text = "工具调用失败，请稍后重试"

                tool_loop_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_use_id,
                    "content": result_text,
                })

            # 重置文本（可能后续轮次产生新文本）
            full_text = ""

    except asyncio.CancelledError:
        # task.cancel() 强制中断：回滚已入历史的 user 消息，保持 user/assistant 成对
        _rollback(session_mgr, user_content)
        raise
    except Exception:
        # LLM 侧失败：回滚已入历史的 user 消息，保持 user/assistant 成对
        # （避免下一轮出现连续 user 消息破坏上下文组装）
        _rollback(session_mgr, user_content)
        raise

    # 7. 落盘（正常完成或协作式取消都会走到这里；被 task.cancel 强制中断则不走到）
    if full_text:
        session_mgr.append_message("assistant", full_text)
        session_mgr.flush()
    elif was_cancelled:
        # 协作式取消且无 token 产出：回滚已入历史的 user 消息，避免孤立 user
        _rollback(session_mgr, user_content)
    else:
        # LLM 返回空回复（罕见但可能）：写入空 assistant 保持消息成对
        session_mgr.append_message("assistant", "")
        session_mgr.flush()


def _build_api_messages(
    config: "Config",
    session_mgr: "SessionManager",
    current_input: str,
    system_content: str,
    tool_loop_messages: Optional[list[dict]] = None,
) -> list[dict]:
    """组装 API messages：system + 历史消息 + tool calling 中间消息

    - system 在最前
    - 历史消息中过滤 tool 角色（WS 不恢复 tool 消息上下文）
    - 最后一条 user 消息换成记忆增强版
    - tool_loop_messages 追加在末尾（当前轮 tool calling 中间产物）
    """
    messages = [{"role": "system", "content": system_content}]
    history = [m for m in session_mgr.get_current_messages() if m.get("role") != "tool"]
    messages.extend(history)
    # 替换最后一条 user 消息为记忆增强版
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "user":
            messages[i] = {"role": "user", "content": current_input}
            break
    # 追加 tool calling 中间消息
    if tool_loop_messages:
        messages.extend(tool_loop_messages)
    return messages


def _default_blocked_message() -> str:
    """BLOCK 拦截无 user_message 时的兜底话术（COMFORT 不走此路径）"""
    from .message_builder import DEFAULT_BLOCKED_MESSAGE
    return DEFAULT_BLOCKED_MESSAGE


def _serialize_tool_calls(tool_calls_data: dict[int, dict]) -> list[dict]:
    """将流式累积的 tool_calls_data 转为 OpenAI API 格式"""
    result = []
    for idx in sorted(tool_calls_data.keys()):
        data = tool_calls_data[idx]
        result.append({
            "id": data["id"],
            "type": "function",
            "function": {
                "name": data["name"],
                "arguments": data["arguments"],
            },
        })
    return result
