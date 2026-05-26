"""Agent 核心模块

纯净的Agent胚子，支持：
- Skill 装配和调用（提示词模版，系统决定注入时机）
- Tool 装配和调用（函数调用，LLM 自主决定）
- MCP 服务器管理（标准化工具协议）
- 对话历史管理
- 日志追踪
"""

from anthropic import Anthropic
from typing import Optional, List, Dict, Any
from ..config import Config
from ..capabilities.skill import SkillBase, SkillContext, SkillResult
from ..capabilities.tool import ToolBase
from ..capabilities.mcp_server import MCPServerBase
from .skill_registry import SkillRegistry, get_registry
from .tool_registry import ToolRegistry
from .mcp_manager import MCPManager
from ..utils.logger import get_logger, generate_trace_id

# tool_use 循环最大轮数，防止无限循环
MAX_TOOL_ROUNDS = 5


class Agent:
    """Agent 核心类（纯净胚子）

    三种能力：
    - Skill（提示词模版）：通过 SkillRegistry 管理
    - Tool（函数调用）：通过 ToolRegistry 管理
    - MCP（标准化工具）：通过 MCPManager 管理，工具注册到 ToolRegistry
    """

    def __init__(self, config: Config):
        self.config = config

        # 初始化 API Client
        self.client = Anthropic(
            api_key=config.api_key,
            base_url=config.base_url
        )

        # 三大能力管理器
        self.registry = get_registry()
        self.tool_registry = ToolRegistry()
        self.mcp_manager = MCPManager()

        # 对话历史
        self.messages: List[Dict] = []

        # 当前 trace_id
        self.trace_id = generate_trace_id()
        self.log = get_logger(self.trace_id)

        self.log.info(f"Agent 初始化: {config.agent.name}")

    # ── Skill ──────────────────────────────────────────────

    def register_skill(self, skill: SkillBase) -> None:
        """注册 Skill（提示词模版）"""
        self.registry.register(skill)
        self.log.info(f"注册 Skill: {skill.meta.name}")

    # ── Tool ───────────────────────────────────────────────

    def register_tool(self, tool: ToolBase) -> None:
        """注册 Tool（函数调用）"""
        self.tool_registry.register_tool(tool)

    # ── MCP ────────────────────────────────────────────────

    def register_mcp(self, server: MCPServerBase) -> None:
        """注册 MCP 服务器"""
        self.mcp_manager.register(server)
        self.tool_registry.register_mcp_server(server)
        self.log.info(f"注册 MCP 服务器: {server.name}")

    # ── 对话 ───────────────────────────────────────────────

    def chat(self, user_input: str, skills: Optional[List[str]] = None) -> str:
        """发送消息并获取回复

        Args:
            user_input: 用户输入
            skills: 要激活的Skill列表（可选）

        Returns:
            Agent回复
        """
        self.trace_id = generate_trace_id()
        self.log = get_logger(self.trace_id)

        self.log.info(f"用户输入: {user_input[:50]}...")

        # 1. Skills 注入提示词（如有）
        message_content = user_input
        if skills:
            message_content = self._execute_skills(user_input, skills)

        # 2. 添加用户消息到历史
        self.messages.append({
            "role": "user",
            "content": message_content
        })

        # 3. 获取所有 tool 定义
        tool_definitions = self.tool_registry.get_all_definitions()

        # 4. 动态调整 max_tokens
        max_tokens = self._calc_dynamic_tokens(user_input)

        # 5. 调用 API（带 tools 参数）
        self.log.info(f"调用 API: {self.config.agent.model}")
        response = self.client.messages.create(
            model=self.config.agent.model,
            max_tokens=max_tokens,
            temperature=self.config.agent.temperature,
            system=self.config.system_prompt,
            messages=self.messages,
            tools=tool_definitions if tool_definitions else None,
        )

        # 6. Tool calling 循环
        tool_rounds = 0
        while self._has_tool_use(response) and tool_rounds < MAX_TOOL_ROUNDS:
            tool_rounds += 1
            tool_results = self._process_tool_calls(response)

            # 将 assistant 的 tool_use 消息和 tool_result 加入历史
            self.messages.append({
                "role": "assistant",
                "content": response.content,
            })
            self.messages.append({
                "role": "user",
                "content": tool_results,
            })

            # 再次调用 API
            self.log.info(f"Tool calling 第 {tool_rounds} 轮")
            response = self.client.messages.create(
                model=self.config.agent.model,
                max_tokens=max_tokens,
                temperature=self.config.agent.temperature,
                system=self.config.system_prompt,
                messages=self.messages,
                tools=tool_definitions if tool_definitions else None,
            )

        # 7. 提取最终文本回复
        assistant_message = self._extract_text(response)

        # 8. 添加助手回复到历史
        self.messages.append({
            "role": "assistant",
            "content": assistant_message
        })

        self.log.info(f"回复完成，长度: {len(assistant_message)}")
        return assistant_message

    # ── 内部方法 ────────────────────────────────────────────

    def _has_tool_use(self, response) -> bool:
        """检查响应是否包含 tool_use"""
        for block in response.content:
            if hasattr(block, 'type') and block.type == 'tool_use':
                return True
        return False

    def _process_tool_calls(self, response) -> list:
        """处理 tool_use 块，返回 tool_result 内容列表"""
        tool_results = []
        for block in response.content:
            if hasattr(block, 'type') and block.type == 'tool_use':
                tool_name = block.name
                tool_input = block.input
                tool_use_id = block.id

                self.log.info(f"调用 Tool: {tool_name}, 参数: {tool_input}")

                try:
                    result_text = self.tool_registry.execute_tool(tool_name, tool_input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_text,
                    })
                except Exception as e:
                    self.log.error(f"Tool {tool_name} 执行失败: {e}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": f"工具调用失败: {e}",
                        "is_error": True,
                    })

        return tool_results

    def _extract_text(self, response) -> str:
        """从响应中提取文本内容"""
        text_content = ""
        for block in response.content:
            if hasattr(block, 'text'):
                text_content += block.text
            elif hasattr(block, 'thinking'):
                pass
            elif hasattr(block, 'content'):
                if isinstance(block.content, str):
                    text_content += block.content
            elif hasattr(block, 'type') and block.type == 'tool_use':
                pass
            else:
                block_type = type(block).__name__
                self.log.warning(f"未知的响应块类型: {block_type}")

        return text_content.strip()

    def _calc_dynamic_tokens(self, user_input: str) -> int:
        """根据用户输入长度动态调整回复长度上限"""
        upper = self.config.agent.max_tokens
        input_len = len(user_input)

        if input_len <= 15:
            return min(256, upper)
        elif input_len <= 50:
            return min(512, upper)
        elif input_len <= 150:
            return min(768, upper)
        else:
            return upper

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
                    self.log.info(f"Skill {skill_name} 已注入prompt")

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
        self.log.info("Agent 资源已清理")
