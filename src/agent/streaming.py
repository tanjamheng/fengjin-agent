"""LLM 流式调用 — 纯工具层（CLI/WS 共用）

职责单一：调用 AsyncOpenAI 流式 API，yield 增量数据。
不做安全检测、记忆检索、上下文组装、Tool Calling 编排、会话管理。
"""

from typing import AsyncGenerator, Optional

from openai import AsyncOpenAI

from .stream_controller import StreamController


async def stream_llm(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    controller: StreamController,
    *,
    tools: Optional[list[dict]] = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> AsyncGenerator[tuple[Optional[str], Optional[list]], None]:
    """纯 LLM 流式调用

    Yields:
        (text_delta, tool_calls_delta) — 至少一个非 None

    text_delta:      文本片段（str 或 None）
    tool_calls_delta: OpenAI 原始 delta.tool_calls 列表（list 或 None）

    取消：
    - controller.cancel_requested → 协作式停止（优雅，不再 yield）
    - asyncio.CancelledError → task.cancel() 强制中断（向上传播）
    """
    params: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    if tools:
        params["tools"] = tools

    stream = await client.chat.completions.create(**params)

    try:
        async for chunk in stream:
            if controller.cancel_requested:
                break

            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            text = delta.content or None
            tool_calls = delta.tool_calls or None

            if text or tool_calls:
                yield text, tool_calls
    finally:
        try:
            await stream.close()
        except Exception:
            pass  # Best-effort close
