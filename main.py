"""CLI 入口"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from src.config import Config, RAGSettings, ContextSettings
from src.agent import Agent
from src.rag.rag_service import RAGService
from src.mcp_servers.rag_server import RAGMCPServer
from src.memory.config import MemorySettings
from src.memory.manager import MemoryManager
from src.agent.context_manager import ContextManager
from src.safety import SafetyManager
from src.utils import setup_logger, LogConfig


def main():
    """主函数"""
    console = Console()

    # 初始化日志系统
    log_config = LogConfig(log_level="INFO", json_format=False)
    setup_logger(log_config)

    # 加载配置
    config_path = Path(__file__).parent / "config" / "config.yaml"
    config = Config.load(str(config_path))

    rag_config_path = Path(__file__).parent / "config" / "rag.yaml"
    rag_config = RAGSettings.load(str(rag_config_path))

    # 加载上下文管理配置
    context_config_path = Path(__file__).parent / "config" / "context.yaml"
    context_settings = ContextSettings.load(str(context_config_path))

    # 初始化记忆系统
    memory_config_path = Path(__file__).parent / "config" / "memory.yaml"
    memory_settings = MemorySettings.load(str(memory_config_path))
    memory_manager = MemoryManager(memory_settings.memory)

    # 创建上下文管理器（依赖记忆检索器）
    context_manager = ContextManager(
        config=context_settings.context,
        memory_retriever=memory_manager
    )

    # 创建 Agent（传入上下文管理器和记忆管理器）
    agent = Agent(
        config=config,
        context_manager=context_manager,
        memory_manager=memory_manager
    )

    # 创建 RAG 服务（纯功能层）
    rag_service = RAGService(rag_config, llm_client=agent.client)

    # 将 RAG 封装为 MCP 服务器并装配到 Agent
    rag_server = RAGMCPServer(rag_service)
    agent.register_mcp(rag_server)

    # 初始化安全护栏（P0 规则引擎 + P1 Llama Guard）
    safety_engine = SafetyManager(
        config_path=str(Path(__file__).parent / "config" / "safety.yaml")
    )

    # 显示欢迎信息
    console.print(Panel.fit(
        f"[bold green]{config.agent.name}[/bold green]\n"
        f"模型: {config.model}\n"
        f"已装配 MCP: [bold cyan]rag[/bold cyan]\n"
        f"上下文管理: [bold cyan]{'启用' if context_settings.context.memory.enabled else '未启用'}[/bold cyan]\n"
        "输入 [bold red]/quit[/bold red] 退出\n"
        "输入 [bold yellow]/clear[/bold yellow] 清空对话历史\n"
        "输入 [bold magenta]/ingest <文件路径>[/bold magenta] 导入文档\n"
        "输入 [bold magenta]/ingest_dir <目录路径>[/bold magenta] 批量导入知识库\n"
        "输入 [bold white]/stats[/bold white] 查看知识库状态\n"
        "输入 [bold cyan]/tools[/bold cyan] 查看可用工具\n"
        "输入 [bold cyan]/skills[/bold cyan] 查看已装配技能\n"
        "输入 [bold cyan]/mcp[/bold cyan] 查看 MCP 服务器",
        title="[Agent 启动]"
    ))

    # 对话循环
    while True:
        try:
            user_input = console.input("[bold blue]你:[/bold blue] ").strip()

            # 处理命令
            if user_input == "/quit":
                agent.cleanup()
                memory_manager.cleanup()
                console.print("[yellow]再见！[/yellow]")
                break

            elif user_input == "/clear":
                agent.clear_history()
                console.print("[green]对话历史已清空[/green]")
                continue

            elif user_input.startswith("/ingest_dir "):
                dir_path = user_input[12:].strip()
                try:
                    result = rag_service.ingest_directory(dir_path, recursive=True)
                    console.print(f"[green]成功导入 {result['document_count']} 个文档，共 {result['total_chunks']} 个文本块[/green]")
                except Exception as e:
                    console.print(f"[red]导入失败: {e}[/red]")
                continue

            elif user_input.startswith("/ingest "):
                file_path = user_input[8:].strip()
                try:
                    result = rag_service.ingest_document(file_path)
                    console.print(f"[green]成功导入文档，生成 {result['chunk_count']} 个文本块[/green]")
                except Exception as e:
                    console.print(f"[red]导入失败: {e}[/red]")
                continue

            elif user_input == "/stats":
                stats = rag_service.get_stats()
                table = Table(title="知识库状态")
                table.add_column("属性", style="cyan")
                table.add_column("值", style="green")
                for key, value in stats.items():
                    table.add_row(str(key), str(value))
                console.print(table)
                continue

            elif user_input == "/tools":
                tools = agent.list_tools()
                if not tools:
                    console.print("[dim]暂无已装配的工具[/dim]")
                else:
                    table = Table(title="可用 Tools")
                    table.add_column("名称", style="cyan")
                    table.add_column("类型", style="yellow")
                    table.add_column("来源", style="magenta")
                    table.add_column("描述", style="white")
                    for tool in tools:
                        table.add_row(
                            tool["name"],
                            tool.get("type", ""),
                            tool.get("source", ""),
                            tool.get("description", "")
                        )
                    console.print(table)
                continue

            elif user_input == "/skills":
                skills = agent.list_skills()
                if not skills:
                    console.print("[dim]暂无已装配的技能[/dim]")
                else:
                    table = Table(title="已装配 Skills")
                    table.add_column("名称", style="cyan")
                    table.add_column("描述", style="white")
                    table.add_column("版本", style="yellow")
                    for skill in skills:
                        table.add_row(skill["name"], skill["description"], skill["version"])
                    console.print(table)
                continue

            elif user_input == "/mcp":
                servers = agent.list_mcp_servers()
                if not servers:
                    console.print("[dim]暂无 MCP 服务器[/dim]")
                else:
                    table = Table(title="MCP 服务器")
                    table.add_column("名称", style="cyan")
                    table.add_column("描述", style="white")
                    table.add_column("已初始化", style="green")
                    table.add_column("工具数", style="yellow")
                    for server in servers:
                        table.add_row(
                            server["name"],
                            server["description"],
                            str(server["initialized"]),
                            str(server["tool_count"])
                        )
                    console.print(table)
                continue

            elif not user_input:
                continue

            # 安全护栏检查
            result = safety_engine.check(user_input)
            if result.blocked:
                msg = result.user_message or "小伊卡发现了一些不太对劲的内容呢~请换个话题吧！"
                console.print(f"[yellow]小伊卡：{msg}[/yellow]")
                continue

            # 发送消息（chat() 内部已流式输出）
            console.print("[bold green]Agent:[/bold green]")
            if result.action.value == "comfort":
                agent.chat(user_input, safety_context=result.comfort_prompt)
            else:
                agent.chat(user_input)
            console.print(f"[dim]对话轮数: {agent.history_count}[/dim]\n")

        except KeyboardInterrupt:
            agent.cleanup()
            memory_manager.cleanup()
            console.print("\n[yellow]再见！[/yellow]")
            break
        except Exception as e:
            console.print(f"[red]错误: {e}[/red]")


if __name__ == "__main__":
    main()
