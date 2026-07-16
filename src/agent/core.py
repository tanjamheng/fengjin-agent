"""Agent 核心 — CLI/WS 唯一对话入口

Agent.chat() = 完整管线（安全→记忆→上下文→LLM→Tool→落盘）
CLI 和 WS 只是 token 的消费方式不同（print vs send_json）。
"""

import asyncio
import json
import time
import uuid
from typing import Optional, Callable

from openai import AsyncOpenAI

from ..config import Config
from ..session import SessionManager, MessageMeta
from ..safety import SafetyManager, Action as SafetyAction
from ..capabilities.skill import SkillBase, SkillContext
from ..capabilities.tool import ToolBase
from ..capabilities.mcp_server import MCPServerBase
from .skill_registry import SkillRegistry, get_registry
from .tool_registry import ToolRegistry
from .mcp_manager import MCPManager
from .context_manager import ContextManager
from .stream_controller import StreamController
from .streaming import stream_llm
from .message_builder import (
    assemble_system_prompt,
    rollback_last_user,
    DEFAULT_BLOCKED_MESSAGE,
    BLOCKED_PREFIX,
)
from ..utils.logger import get_logger, generate_trace_id


# ── 常量 ──────────────────────────────────────────────────

MAX_INPUT_LENGTH = 10000  # 超长输入拒绝（对齐 CLAUDE.md 技术约束）


def _extract_rag_sources(result_text: str) -> list[str]:
    """从 RAG 工具结果的来源标签中提取命中文档。"""
    sources = []
    for line in result_text.splitlines():
        if line.startswith("[来源: ") and line.endswith("]"):
            source = line[len("[来源: "):-1].strip()
            if source and source not in sources:
                sources.append(source)
    return sources

# ── 异常 ──────────────────────────────────────────────────

class BlockedError(Exception):
    """安全拦截（消息已入历史，caller 负责展示拦截话术）"""

    def __init__(self, message: str, category: str):
        super().__init__(message)
        self.message = message
        self.category = category


class StreamInterrupted(Exception):
    """Token 流式输出被中断（客户端断开）。不回滚 user 消息，保留部分回复。"""
    pass


# ── Agent ─────────────────────────────────────────────────

class Agent:
    """Agent 核心类

    持有配置、客户端、工具/技能/MCP 注册表。
    chat() 是唯一对话入口——CLI 和 WS 共用同一管线。
    """

    def __init__(
        self,
        config: Config,
        session_mgr: SessionManager,
        safety: SafetyManager,
        *,
        client: Optional[AsyncOpenAI] = None,
        context_manager: Optional[ContextManager] = None,
        memory_manager=None,
        mind_manager=None,
        on_mind_warning: Optional[Callable] = None,
        tool_registry: Optional[ToolRegistry] = None,
        mood_engine=None,
        bond_tracker=None,
        persona_guard=None,
    ):
        self.config = config
        self.session_mgr = session_mgr
        self.safety = safety
        self.context_manager = context_manager
        self.memory_manager = memory_manager
        self.mind_manager = mind_manager
        self.on_mind_warning = on_mind_warning
        self.mood_engine = mind_manager.mood_engine if mind_manager else mood_engine
        self.bond_tracker = mind_manager.bond_tracker if mind_manager else bond_tracker
        self.persona_guard = persona_guard

        # 角色漂移：本轮检测 → 下轮注入
        self._pending_anchor: Optional[str] = None

        # AsyncOpenAI — 可注入（WS 从 app.state 共享）或自动创建（CLI）
        if client is None:
            config.validate_main_model_config()
            self.client = AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=120.0,
                max_retries=3,
            )
        else:
            self.client = client

        # 三大能力管理器
        self.registry = get_registry()
        self.tool_registry = tool_registry or ToolRegistry()
        self.mcp_manager = MCPManager()

        # 当前 trace_id + 流控制
        self.trace_id = generate_trace_id()
        self._current_controller: Optional[StreamController] = None
        self.log = get_logger("core", trace_id=self.trace_id)

        self.log.info("Agent 初始化: {} (AsyncOpenAI)", config.agent.name)

    # ── Skill / Tool / MCP 注册 ────────────────────────────

    def register_skill(self, skill: SkillBase) -> None:
        """注册 Skill（提示词模版）"""
        self.registry.register(skill)
        self.log.info("注册 Skill: {}", skill.meta.name)

    def register_tool(self, tool: ToolBase) -> None:
        """注册 Tool（函数调用）"""
        self.tool_registry.register_tool(tool)

    def register_mcp(self, server: MCPServerBase) -> None:
        """注册 MCP 服务器"""
        self.mcp_manager.register(server)
        self.tool_registry.register_mcp_server(server)
        self.log.info("注册 MCP 服务器: {}", server.name)

    # ── 取消 ────────────────────────────────────────────────

    def cancel(self) -> None:
        """协作式取消当前对话（保留已生成文本）"""
        if self._current_controller:
            self._current_controller.cancel()

    # ── chat() — 唯一对话入口（CLI/WS 共用）─────────────────

    async def chat(
        self,
        user_input: str,
        *,
        trace_id: str = "",
        on_token: Optional[Callable] = None,
        skills: Optional[list[str]] = None,
        _client_snapshot=None,
        _config_snapshot=None,
    ) -> str:
        """完整对话管线

        安全检测 → 心智注入 → 上下文组装 → LLM 流式 → Tool Calling → 落盘 → 异步心智处理

        Args:
            user_input: 用户输入
            trace_id: 请求追踪 ID（不传则自动生成）
            on_token: 流式 token 回调（可 sync 或 async）
            skills: 要激活的 Skill 名称列表（可选）

        Returns:
            AI 完整回复文本

        Raises:
            BlockedError: 安全拦截（消息已入历史）
            ValueError: 输入超过 {} 字符限制
        """
        if not user_input or not user_input.strip():
            return ""
        if len(user_input) > MAX_INPUT_LENGTH:
            raise ValueError(
                f"输入过长（{len(user_input)}字符），请限制在 {MAX_INPUT_LENGTH} 字符以内"
            )
        # 热更新只影响下一轮对话；一次 Tool Calling 链始终使用同一客户端和配置。
        chat_config = _config_snapshot or self.config
        chat_client = _client_snapshot or self.client
        chat_config.validate_main_model_config()

        self.trace_id = trace_id or generate_trace_id()
        logger = get_logger("core", trace_id=self.trace_id)
        t_total_start = time.monotonic()

        logger.info("收到用户输入 ({} chars)", len(user_input))

        # 1. Skill 注入（如有）
        message_content = user_input
        if skills:
            logger.info("Skill 注入: {} 个 Skill → {}", len(skills), skills)
            t_skill_start = time.monotonic()
            message_content = self._execute_skills(user_input, skills)
            t_skill = (time.monotonic() - t_skill_start) * 1000
            logger.info("Skill 注入完成 ({:.0f}ms)", t_skill)

        # 2. 用户消息入历史（无论安全判定如何，核心1 2.5）
        self.session_mgr.append_message("user", user_input)
        logger.debug("用户原始消息已入历史 ({} chars)", len(user_input))

        # 提前创建 controller + 赋值，消除 cancel 信号丢失窗口
        controller = StreamController()
        self._current_controller = controller

        try:
            # 3. 安全检测（三态分流：P0 规则引擎 → P1 Llama Guard）
            t_safety_start = time.monotonic()
            # Skill 增强文本不是用户原话，不能替代原始输入的安全边界。
            result = self.safety.check(user_input, trace_id=self.trace_id)
            t_safety = (time.monotonic() - t_safety_start) * 1000
            # 安全检测详情由 SafetyManager 内部日志输出（P0/P1 各自耗时）

            if result.action == SafetyAction.BLOCK:
                blocked_msg = result.user_message or DEFAULT_BLOCKED_MESSAGE
                self.session_mgr.append_message(
                    "assistant",
                    f"{BLOCKED_PREFIX} {blocked_msg}",
                )
                self.session_mgr.flush()
                logger.info("对话已拦截 (category={})，总耗时 {:.0f}ms",
                            result.category,
                            (time.monotonic() - t_total_start) * 1000)
                raise BlockedError(blocked_msg, result.category)

            # COMFORT：放行，安抚指令注入 system_prompt（红线10）
            comfort_prompt = (
                result.comfort_prompt
                if result.action == SafetyAction.COMFORT
                else None
            )
            if comfort_prompt:
                logger.info("COMFORT 模式已激活: 自伤安抚指令将注入 system_prompt")

            # 4. 角色校准 → 心智状态/记忆注入 → 上下文组装
            t_memory_start = time.monotonic()
            api_input = message_content
            memory_used: list[str] = []
            # 角色漂移锚点（上一轮检测到偏离 → 本轮注入）
            if self._pending_anchor:
                api_input = self._pending_anchor + "\n\n" + api_input
                self._pending_anchor = None
            if self.mind_manager:
                api_input = self.mind_manager.inject_state(api_input)
            else:
                if self.bond_tracker:
                    api_input = self.bond_tracker.inject(api_input)
                if self.mood_engine:
                    api_input = self.mood_engine.inject(api_input)
            if self.context_manager:
                api_input, memory_used = self.context_manager.build_input_with_metadata(
                    api_input, trace_id=self.trace_id
                )
            t_memory = (time.monotonic() - t_memory_start) * 1000
            logger.info("心智注入+上下文组装完成 ({:.0f}ms)", t_memory)
        except BlockedError:
            raise
        except Exception:
            rollback_last_user(self.session_mgr, user_input)
            raise

        # 5. Tool Calling 流水线
        full_text = ""
        all_text = ""  # 跨 Tool Calling 轮次累积（StreamInterrupted 时保存完整内容）
        tool_loop_messages: list[dict] = []
        tool_rounds = 0
        rag_hits: list[str] = []
        total_tokens = 0
        tool_definitions = self.tool_registry.get_all_definitions()
        max_tool_rounds = (
            chat_config.agent.max_tool_rounds if tool_definitions else 0
        )

        async def _execute_tool(name: str, args: dict) -> str:
            return await asyncio.to_thread(
                self.tool_registry.execute_tool, name, args, self.trace_id
            )

        try:
            while True:
                if controller.cancel_requested:
                    logger.info("工具链继续前检测到取消信号")
                    break
                # 5a. 组装上下文 + 滑动窗口
                t_build_start = time.monotonic()
                system_content = assemble_system_prompt(
                    chat_config, comfort_prompt
                )
                api_messages = _build_api_messages(
                    self.session_mgr,
                    api_input,
                    system_content,
                    tool_loop_messages,
                )
                # 剥离 system 再裁剪
                system_msg = (
                    api_messages[0]
                    if api_messages and api_messages[0].get("role") == "system"
                    else None
                )
                trim_target = (
                    api_messages[1:] if system_msg else api_messages
                )

                # 裁剪前统计
                pre_trim_count = len(trim_target)
                pre_trim_tokens = (
                    self.context_manager._estimate_tokens(trim_target)
                    if self.context_manager else 0
                )

                if self.context_manager:
                    trim_target = self.context_manager.trim_messages(
                        trim_target, trace_id=self.trace_id
                    )

                # 裁剪后统计
                post_trim_count = len(trim_target)
                post_trim_tokens = (
                    self.context_manager._estimate_tokens(trim_target)
                    if self.context_manager else pre_trim_tokens
                )
                trimmed = pre_trim_count - post_trim_count

                api_messages = (
                    [system_msg] + trim_target if system_msg else trim_target
                )
                t_build = (time.monotonic() - t_build_start) * 1000
                if trimmed > 0:
                    logger.info(
                        "调用 LLM: {} ({} 条消息, 裁剪 {}→{}, "
                        "~{}→{} tk, 组装 {:.0f}ms)",
                        chat_config.model, len(api_messages),
                        pre_trim_count, post_trim_count,
                        pre_trim_tokens, post_trim_tokens, t_build,
                    )
                else:
                    logger.info(
                        "调用 LLM: {} ({} 条消息, ~{} tk, 组装 {:.0f}ms)",
                        chat_config.model, len(api_messages),
                        pre_trim_tokens, t_build,
                    )

                # 5b. 流式调用 LLM
                t_llm_start = time.monotonic()
                first_token = False
                tool_calls_data: dict[int, dict] = {}

                async for text_delta, tc_delta in stream_llm(
                    client=chat_client,
                    model=chat_config.model,
                    messages=api_messages,
                    controller=controller,
                    tools=tool_definitions if tool_definitions else None,
                    temperature=chat_config.agent.temperature,
                    max_tokens=chat_config.agent.max_tokens,
                ):
                    # 文本 token
                    if text_delta:
                        if not first_token:
                            t_ttft = (
                                time.monotonic() - t_llm_start
                            ) * 1000
                            logger.info("LLM 首 token (TTFT): {:.0f}ms",
                                        t_ttft)
                            first_token = True
                        total_tokens += 1
                        full_text += text_delta
                        if on_token is not None:
                            result_cb = on_token(text_delta)
                            if asyncio.iscoroutine(result_cb):
                                await result_cb

                    # tool_calls 增量累积
                    if tc_delta:
                        _accumulate_tool_calls(tool_calls_data, tc_delta)

                t_llm = (time.monotonic() - t_llm_start) * 1000
                tps = total_tokens / (t_llm / 1000) if t_llm > 0 else 0
                logger.info("LLM 流式完成: {} tokens, {:.0f}ms ({:.1f} tok/s)",
                            total_tokens, t_llm, tps)

                if controller.cancel_requested:
                    logger.info("用户取消回复 (已生成 {} tokens)", total_tokens)
                    break

                # 5c. Tool Calling
                if not tool_calls_data:
                    if tool_rounds == 0 and tool_definitions:
                        available_tools = [
                            item.get("function", {}).get("name", "")
                            for item in tool_definitions
                        ]
                        logger.info(
                            "LLM 本轮未调用工具（可用工具: {}）",
                            ", ".join(name for name in available_tools if name),
                        )
                    break
                if tool_rounds >= max_tool_rounds:
                    logger.warning("Tool Calling 达到最大轮数 {}，停止调用", max_tool_rounds)
                    break

                tool_rounds += 1
                logger.info("Tool calling 第 {} 轮，{} 个工具调用",
                            tool_rounds, len(tool_calls_data))

                tool_calls_serialized = _serialize_tool_calls(
                    tool_calls_data
                )
                tool_loop_messages.append({
                    "role": "assistant",
                    "content": full_text or "",
                    "tool_calls": tool_calls_serialized,
                })

                t_tools_start = time.monotonic()
                for idx in sorted(tool_calls_data.keys()):
                    if controller.cancel_requested:
                        break
                    tc = tool_calls_data[idx]
                    tool_name = tc["name"]
                    try:
                        tool_input = json.loads(tc["arguments"])
                    except json.JSONDecodeError:
                        logger.warning(
                            "Tool {} 参数 JSON 解析失败: {}",
                            tool_name, tc["arguments"][:100],
                        )
                        tool_input = {}

                    try:
                        t_tool_start = time.monotonic()
                        result_text = await _execute_tool(
                            tool_name, tool_input
                        )
                        if tool_name == "rag_retrieve":
                            rag_hits.extend(_extract_rag_sources(result_text))
                        if controller.cancel_requested:
                            break
                        t_tool = (
                            time.monotonic() - t_tool_start
                        ) * 1000
                        logger.info("Tool {} 执行成功 ({:.0f}ms)",
                                    tool_name, t_tool)
                    except Exception as e:
                        logger.error("Tool {} 执行失败: {}",
                                     tool_name, e)
                        result_text = "工具调用失败，请稍后重试"

                    tool_loop_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"] or f"tc_{uuid.uuid4().hex[:8]}",
                        "content": result_text,
                    })
                t_tools_total = (time.monotonic() - t_tools_start) * 1000
                logger.info("Tool calling 第 {} 轮完成 ({:.0f}ms)",
                            tool_rounds, t_tools_total)

                all_text += full_text
                full_text = ""
                total_tokens = 0

        except StreamInterrupted:
            # 流式输出中断（客户端断开）：保留 user 消息 + 完整回复（含前几轮 Tool Calling 中间文本）
            logger.info("流式输出中断 (客户端断开), 已生成 {} chars", len(all_text + full_text))
            combined = all_text + full_text
            if combined:
                logger.info("保存部分回复 ({} chars) + 触发异步记忆提取", len(combined))
                self.session_mgr.append_message(
                    "assistant", combined,
                    MessageMeta(
                        emotion=self._current_emotion_metadata(),
                        rag_hits=rag_hits or None,
                        memory_used=memory_used or None,
                    ),
                )
                self.session_mgr.flush()
                if self.mind_manager:
                    self.mind_manager.submit(
                        self.session_mgr.get_current_messages(raw_user_content=True), self.trace_id,
                        self.on_mind_warning, include_state=False,
                    )
                elif self.memory_manager:
                    self.memory_manager.extract_async(
                        user_input, combined, trace_id=self.trace_id
                    )
            raise
        except asyncio.CancelledError:
            logger.info("对话任务被取消, 回滚用户消息")
            rollback_last_user(self.session_mgr, user_input)
            raise
        except Exception:
            logger.exception("对话管线异常, 回滚用户消息")
            rollback_last_user(self.session_mgr, user_input)
            raise
        finally:
            self._current_controller = None

        if all_text:
            full_text = all_text + full_text

        # 6. 角色漂移检测（本轮回复 → 下一轮注入）
        if self.persona_guard and full_text:
            try:
                self._pending_anchor = self.persona_guard.check(
                    full_text, trace_id=self.trace_id
                )
            except Exception:
                logger.error("角色漂移检测失败，跳过本轮")
                self._pending_anchor = None

        # 7. 落盘
        if controller.cancel_requested:
            # full_text 已在上方合并 all_text，直接保留即可，避免 Tool Calling 前文重复。
            combined = full_text
            if combined:
                self.session_mgr.append_message(
                    "assistant", combined,
                    MessageMeta(
                        emotion=self._current_emotion_metadata(),
                        rag_hits=rag_hits or None,
                        memory_used=memory_used or None,
                    ),
                )
                self.session_mgr.flush()
                logger.info("用户取消: 保留已生成内容 ({} chars)", len(combined))
            else:
                rollback_last_user(self.session_mgr, user_input)
                logger.info("用户取消: 无内容，用户消息已回滚")
        elif full_text:
            self.session_mgr.append_message(
                "assistant", full_text,
                MessageMeta(
                    emotion=self._current_emotion_metadata(),
                    rag_hits=rag_hits or None,
                    memory_used=memory_used or None,
                ),
            )
            self.session_mgr.flush()
            logger.info("会话已落盘 (回复 {} chars)", len(full_text))
        else:
            self.session_mgr.append_message("assistant", "")
            self.session_mgr.flush()
            logger.warning("回复为空, 空消息已落盘")

        # 8. 异步心智处理（记忆 + 情绪羁绊，不阻塞回复）
        if self.mind_manager and full_text:
            logger.debug("触发异步心智处理")
            self.mind_manager.submit(
                self.session_mgr.get_current_messages(raw_user_content=True), self.trace_id,
                self.on_mind_warning,
                include_state=not controller.cancel_requested,
            )
        elif self.memory_manager and full_text:
            self.memory_manager.extract_async(
                user_input, full_text, trace_id=self.trace_id
            )
        elif self.memory_manager:
            logger.debug("跳过记忆提取: 回复为空")
        else:
            logger.debug("跳过记忆提取: 记忆系统未启用")

        t_total = (time.monotonic() - t_total_start) * 1000
        logger.info("对话完成: {} chars, {} tokens, {:.0f}ms 总耗时 (安全 {:.0f}ms + 上下文 {:.0f}ms + LLM {:.0f}ms)",
                    len(full_text), total_tokens, t_total,
                    t_safety, t_memory, t_llm)
        return full_text

    def _current_emotion_metadata(self) -> Optional[str]:
        """记录生成本轮回复时实际注入的情绪状态。"""
        if self.mind_manager and not self.mind_manager.active:
            return None
        if not self.mood_engine:
            return None
        try:
            return self.mood_engine.describe()
        except Exception as exc:
            self.log.warning("读取情绪元数据失败，已跳过: {}", exc)
            return None

    # ── 管理方法 ────────────────────────────────────────────

    def clear_history(self) -> None:
        """清空对话历史（创建新会话）"""
        self.session_mgr.flush()
        self.session_mgr.create_session()
        self._pending_anchor = None  # 角色校准不跨会话
        self.log.info("对话历史已清空")

    @property
    def history_count(self) -> int:
        return len(self.session_mgr.get_current_messages()) // 2

    def list_skills(self) -> list[dict]:
        """列出已注册的 Skills"""
        skills = self.registry.list_skills()
        return [
            {
                "name": s.name,
                "description": s.description,
                "version": s.version,
            }
            for s in skills
        ]

    def list_tools(self) -> list[dict]:
        """列出已注册的 Tools"""
        return self.tool_registry.list_tools()

    def list_mcp_servers(self) -> list[dict]:
        """列出已注册的 MCP 服务器"""
        return self.mcp_manager.list_servers()

    def cleanup(self) -> None:
        """清理所有资源"""
        self.registry.cleanup_all()
        self.mcp_manager.cleanup_all()
        self.tool_registry.clear()
        if self.mind_manager:
            self.mind_manager.cleanup()
        try:
            asyncio.run(self.client.close())
        except RuntimeError:
            # Event loop 已在运行（如 WS 路径调用 cleanup），跳过
            pass
        except Exception as e:
            self.log.warning("AsyncOpenAI client 关闭异常: {}", e)
        self.log.info("Agent 资源已清理")

    # ── 内部方法 ────────────────────────────────────────────

    def _execute_skills(self, user_input: str, skill_names: list[str]) -> str:
        """执行 Skills 并返回处理后的 prompt（链式执行）"""
        conversation_history = self.session_mgr.get_current_messages()
        context = SkillContext(
            trace_id=self.trace_id,
            user_input=user_input,
            conversation_history=conversation_history,
            config={},
        )
        current_prompt = user_input
        for skill_name in skill_names:
            result = self.registry.execute(skill_name, context)
            if result.success and result.data:
                if "prompt" in result.data:
                    current_prompt = result.data["prompt"]
                    self.log.info("Skill {} 已注入 prompt", skill_name)
        return current_prompt


# ── 模块级工具函数 ──────────────────────────────────────────

def _build_api_messages(
    session_mgr: SessionManager,
    current_input: str,
    system_content: str,
    tool_loop_messages: Optional[list[dict]] = None,
) -> list[dict]:
    """组装 API messages：system + 历史 + tool calling 中间消息"""
    messages = [{"role": "system", "content": system_content}]
    # 过滤 tool 消息 + 被拦截消息对（核心1 2.5：被拦截消息不送入 AI）
    raw = session_mgr.get_current_messages()
    history = []
    i = 0
    while i < len(raw):
        m = raw[i]
        if m.get("role") == "tool":
            i += 1
            continue
        if (m["role"] == "user" and i + 1 < len(raw)
            and raw[i + 1].get("role") == "assistant"
            and raw[i + 1].get("content", "").startswith(BLOCKED_PREFIX)):
            i += 2  # 跳过被拦截的 user 消息 + 小伊卡通知
            continue
        history.append(m)
        i += 1
    messages.extend(history)
    # 替换最后一条 user 消息为记忆增强版
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "user":
            messages[i] = {"role": "user", "content": current_input}
            break
    if tool_loop_messages:
        messages.extend(tool_loop_messages)
    return messages


def _accumulate_tool_calls(
    tool_calls_data: dict[int, dict],
    tc_delta_list: list,
) -> None:
    """增量累积流式 tool_calls delta"""
    for tc_delta in tc_delta_list:
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


def _serialize_tool_calls(tool_calls_data: dict[int, dict]) -> list[dict]:
    """将流式累积的 tool_calls_data 转为 OpenAI API 格式"""
    result = []
    for idx in sorted(tool_calls_data.keys()):
        data = tool_calls_data[idx]
        tc_id = data["id"] or f"tc_{uuid.uuid4().hex[:8]}"
        result.append({
            "id": tc_id,
            "type": "function",
            "function": {
                "name": data["name"],
                "arguments": data["arguments"],
            },
        })
    return result
