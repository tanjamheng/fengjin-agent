"""Agent 核心模块

纯净的Agent胚子，支持：
- Skill 装配和调用（提示词模版，系统决定注入时机）
- Tool 装配和调用（函数调用，LLM 自主决定）
- MCP 服务器管理（标准化工具协议）
- 上下文管理（记忆合并 + 滑动窗口）
- 对话历史管理
- 日志追踪
"""

import json
from openai import OpenAI
from typing import Optional, List, Dict, Any
from ..config import Config
from ..capabilities.skill import SkillBase, SkillContext, SkillResult
from ..capabilities.tool import ToolBase
from ..capabilities.mcp_server import MCPServerBase
from .skill_registry import SkillRegistry, get_registry
from .tool_registry import ToolRegistry
from .mcp_manager import MCPManager
from .context_manager import ContextManager
from .message_builder import assemble_system_prompt
from ..utils.logger import get_logger, generate_trace_id


class Agent:
    """Agent 核心类（纯净胚子）

    三种能力：
    - Skill（提示词模版）：通过 SkillRegistry 管理
    - Tool（函数调用）：通过 ToolRegistry 管理
    - MCP（标准化工具）：通过 MCPManager 管理，工具注册到 ToolRegistry
    """

    def __init__(self, config: Config, context_manager: ContextManager = None, memory_manager=None):
        self.config = config

        # 初始化 API Client
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=120.0,
            max_retries=3,
        )

        # 三大能力管理器
        self.registry = get_registry()
        self.tool_registry = ToolRegistry()
        self.mcp_manager = MCPManager()

        # 上下文管理 + 记忆管理
        self.context_manager = context_manager
        self.memory_manager = memory_manager

        # 对话历史
        self.messages: List[Dict] = []

        # 当前 trace_id
        self.trace_id = generate_trace_id()
        self.log = get_logger("core", trace_id=self.trace_id)

        self.log.info("Agent 初始化: {}", config.agent.name)

    # ── Skill ──────────────────────────────────────────────

    def register_skill(self, skill: SkillBase) -> None:
        """注册 Skill（提示词模版）"""
        self.registry.register(skill)
        self.log.info("注册 Skill: {}", skill.meta.name)

    # ── Tool ───────────────────────────────────────────────

    def register_tool(self, tool: ToolBase) -> None:
        """注册 Tool（函数调用）"""
        self.tool_registry.register_tool(tool)

    # ── MCP ────────────────────────────────────────────────

    def register_mcp(self, server: MCPServerBase) -> None:
        """注册 MCP 服务器"""
        self.mcp_manager.register(server)
        self.tool_registry.register_mcp_server(server)
        self.log.info("注册 MCP 服务器: {}", server.name)

    # ── 对话 ───────────────────────────────────────────────

    def chat(self, user_input: str, skills: Optional[List[str]] = None,
             safety_context: Optional[str] = None, trace_id: str = "") -> str:
        """发送消息并获取回复

        Args:
            user_input: 用户输入
            skills: 要激活的Skill列表（可选）
            safety_context: 安全疏导指令（comfort 模式时注入）
            trace_id: 请求追踪ID（不传则自动生成）

        Returns:
            Agent回复
        """
        # 输入防御性校验
        if not user_input or not user_input.strip():
            return ""

        self.trace_id = trace_id or generate_trace_id()
        self.log = get_logger("core", trace_id=self.trace_id)

        self.log.info("用户输入: {}...", user_input[:50])

        # 1. Skills 注入提示词（如有）
        message_content = user_input
        if skills:
            message_content = self._execute_skills(user_input, skills)

        # 2. 上下文管理：记忆合并到当前输入
        api_input = message_content
        if self.context_manager:
            api_input = self.context_manager.build_input(message_content, trace_id=self.trace_id)

        # 3. 存入历史的是原始输入（不含记忆注入）
        self.messages.append({
            "role": "user",
            "content": message_content
        })

        # 4. 获取所有 tool 定义
        tool_definitions = self.tool_registry.get_all_definitions()

        # 5. 构建 API 参数（system prompt 置顶于 messages）
        system_prompt = assemble_system_prompt(self.config, safety_context)

        api_messages = [{"role": "system", "content": system_prompt}]
        api_messages.extend(self._build_api_messages(api_input))

        api_params = {
            "model": self.config.model,
            "max_tokens": self.config.agent.max_tokens,
            "temperature": self.config.agent.temperature,
            "messages": api_messages,
            "tools": tool_definitions if tool_definitions else None,
        }

        # 6. 流式调用 API
        self.log.info("调用 API: {}", self.config.model)
        response = self._stream_call(api_params)

        # 7. Tool calling 循环
        tool_rounds = 0
        while self._has_tool_use(response) and tool_rounds < self.config.agent.max_tool_rounds:
            tool_rounds += 1
            tool_calls, tool_messages = self._process_tool_calls(response)

            # assistant 消息（含 tool_calls）
            self.messages.append({
                "role": "assistant",
                "content": response.choices[0].message.content or "",
                "tool_calls": tool_calls,
            })
            # tool 结果消息（每条独立）
            self.messages.extend(tool_messages)

            self.log.info("Tool calling 第 {} 轮", tool_rounds)
            api_params["messages"] = self._build_api_messages_with_system(system_prompt, api_input)
            response = self._stream_call(api_params)

        if tool_rounds >= self.config.agent.max_tool_rounds and self._has_tool_use(response):
            self.log.warning("Tool calling 达到 {} 轮上限，强制终止", self.config.agent.max_tool_rounds)

        # 8. 提取最终文本回复
        assistant_message = self._extract_text(response)

        # 9. 添加助手回复到历史（跳过空消息，如Tool Calling达上限时仅有tool_calls无text）
        if assistant_message:
            self.messages.append({
                "role": "assistant",
                "content": assistant_message
            })

        # 10. 滑动窗口裁剪
        if self.context_manager:
            self.context_manager.trim_messages(self.messages, trace_id=self.trace_id)

        # 11. 异步提取记忆
        if self.memory_manager:
            self.memory_manager.extract_async(user_input, assistant_message,
                                              trace_id=self.trace_id)

        self.log.info("回复完成，长度: {}", len(assistant_message))
        return assistant_message

    # ── 内部方法 ────────────────────────────────────────────

    def _build_api_messages(self, current_input: str) -> list:
        """构建首次 API 调用的 messages（不含 system）

        = 历史消息（self.messages[:-1]）+ 当前输入（合并了记忆）
        self.messages 里存的是原始输入，这里用合并后的版本替换最后一条
        """
        api_messages = [m.copy() for m in self.messages[:-1]]
        api_messages.append({"role": "user", "content": current_input})
        return api_messages

    def _build_api_messages_from_history(self, enhanced_input: str = None) -> list:
        """构建 tool calling 后续轮次的 messages（不含 system）

        若提供 enhanced_input，替换最后一条 user 消息为记忆增强版本。
        """
        messages = [m.copy() for m in self.messages]
        if enhanced_input is not None:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i]["role"] == "user":
                    messages[i] = {"role": "user", "content": enhanced_input}
                    break
        return messages

    def _build_api_messages_with_system(self, system_prompt: str, enhanced_input: str = None) -> list:
        """构建带 system prompt 的完整 messages"""
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self._build_api_messages_from_history(enhanced_input))
        return messages

    def _stream_call(self, api_params: dict):
        """流式调用 API，实时输出文本，返回模拟的统一响应对象"""
        import sys
        safe_stdout = getattr(sys.stdout, 'reconfigure', None)
        if safe_stdout:
            sys.stdout.reconfigure(errors='replace')

        stream = None
        try:
            stream = self.client.chat.completions.create(**api_params, stream=True)

            full_text = ""
            tool_calls_data = {}  # index -> {id, name, arguments}

            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # 文本内容
                if delta.content:
                    full_text += delta.content
                    print(delta.content, end="", flush=True)

                # tool_calls 增量
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

            print()  # 流式结束后换行

            return self._build_response(full_text, tool_calls_data)

        except Exception as e:
            self.log.opt(exception=True).error("API调用失败: {}", e)
            raise
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception as e:
                    self.log.debug("流关闭异常: {}", e)

    def _build_response(self, text: str, tool_calls_data: dict):
        """构造统一的响应对象，兼容 _has_tool_use / _process_tool_calls / _extract_text"""

        class FunctionCall:
            def __init__(self, name, arguments):
                self.name = name
                self.arguments = arguments

        class ToolCall:
            def __init__(self, id, function):
                self.id = id
                self.type = "function"
                self.function = function

        class Message:
            def __init__(self, content, tool_calls):
                self.content = content
                self.tool_calls = tool_calls

        class Choice:
            def __init__(self, message):
                self.message = message

        class Response:
            def __init__(self, choices):
                self.choices = choices

        tool_calls = []
        for idx in sorted(tool_calls_data.keys()):
            data = tool_calls_data[idx]
            tool_calls.append(ToolCall(
                id=data["id"],
                function=FunctionCall(data["name"], data["arguments"])
            ))

        message = Message(content=text, tool_calls=tool_calls if tool_calls else None)
        return Response(choices=[Choice(message=message)])

    def _has_tool_use(self, response) -> bool:
        """检查响应是否包含 tool_calls"""
        msg = response.choices[0].message
        return msg.tool_calls is not None and len(msg.tool_calls) > 0

    def _process_tool_calls(self, response) -> tuple:
        """处理 tool_calls，返回 (tool_calls_list, tool_messages_list)

        tool_calls_list: assistant 消息中的 tool_calls 字段
        tool_messages_list: 每条 tool 结果的独立消息
        """
        msg = response.choices[0].message
        tool_calls_list = []
        tool_messages = []

        for tc in msg.tool_calls:
            tool_name = tc.function.name
            tool_use_id = tc.id
            try:
                tool_input = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                self.log.warning("Tool {} 参数 JSON 解析失败: {}", tool_name,
                                 tc.function.arguments[:100])
                tool_input = {}

            self.log.info("调用 Tool: {}, 参数: {}", tool_name, tool_input)

            tool_calls_list.append({
                "id": tool_use_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": tc.function.arguments,
                },
            })

            try:
                result_text = self.tool_registry.execute_tool(tool_name, tool_input)
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_use_id,
                    "content": result_text,
                })
            except Exception as e:
                self.log.error("Tool {} 执行失败: {}", tool_name, e)
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_use_id,
                    "content": "工具调用失败，请稍后重试",
                })

        return tool_calls_list, tool_messages

    def _extract_text(self, response) -> str:
        """从响应中提取文本内容"""
        msg = response.choices[0].message
        return (msg.content or "").strip()

    def _execute_skills(self, user_input: str, skill_names: List[str]) -> str:
        """执行Skills并返回处理后的prompt"""
        context = SkillContext(
            trace_id=self.trace_id,
            user_input=user_input,
            conversation_history=self.messages,
            config={}
        )

        current_prompt = user_input

        for skill_name in skill_names:
            result = self.registry.execute(skill_name, context)
            if result.success and result.data:
                if "prompt" in result.data:
                    current_prompt = result.data["prompt"]
                    self.log.info("Skill {} 已注入prompt", skill_name)

        return current_prompt

    # ── 管理 ────────────────────────────────────────────────

    def clear_history(self) -> None:
        """清空对话历史"""
        self.messages = []
        self.log.info("对话历史已清空")

    @property
    def history_count(self) -> int:
        return len(self.messages) // 2

    def list_skills(self) -> List[dict]:
        """列出已注册的 Skills"""
        skills = self.registry.list_skills()
        return [{"name": s.name, "description": s.description, "version": s.version} for s in skills]

    def list_tools(self) -> List[dict]:
        """列出已注册的 Tools"""
        return self.tool_registry.list_tools()

    def list_mcp_servers(self) -> List[dict]:
        """列出已注册的 MCP 服务器"""
        return self.mcp_manager.list_servers()

    def cleanup(self) -> None:
        """清理所有资源"""
        self.registry.cleanup_all()
        self.mcp_manager.cleanup_all()
        self.tool_registry.clear()
        try:
            self.client.close()
        except Exception as e:
            self.log.warning("OpenAI client 关闭异常: {}", e)
        self.log.info("Agent 资源已清理")
