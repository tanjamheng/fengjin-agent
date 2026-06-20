"""CLI 入口"""

import logging
import os
import sys
import warnings
from pathlib import Path

# ── 抑制第三方库噪音 + 加速初始化（必须在 import 之前设置）──
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"           # TensorFlow C++: 只显示 FATAL
os.environ["TOKENIZERS_PARALLELISM"] = "false"      # tokenizers: 禁止并行化警告
os.environ["TRANSFORMERS_VERBOSITY"] = "error"      # transformers: 只显示错误
os.environ["CUDA_MODULE_LOADING"] = "LAZY"          # PyTorch: 延迟加载 CUDA 模块
# HF_HUB_OFFLINE 和 TRANSFORMERS_OFFLINE 在 ensure_models() 完成后设置

# 抑制第三方库 Python logger
logging.getLogger("tensorflow").setLevel(logging.ERROR)
logging.getLogger("tf_keras").setLevel(logging.ERROR)
logging.getLogger("transformers").setLevel(logging.ERROR)

warnings.filterwarnings("ignore", module="tensorflow.*")
warnings.filterwarnings("ignore", module="tf_keras.*")
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*torch_dtype.*")
warnings.filterwarnings("ignore", message=".*attention mask.*")
warnings.filterwarnings("ignore", message=".*pad_token_id.*")
warnings.filterwarnings("ignore", message=".*generation flags.*")
warnings.filterwarnings("ignore", message=".*meta device.*")

sys.path.insert(0, str(Path(__file__).parent))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from src.config import Config, RAGSettings, ContextSettings
from src.agent import Agent
from src.rag.rag_service import RAGService
from src.mcp_servers.rag_server import RAGMCPServer
from src.memory.config import MemorySettings
from src.memory.manager import MemoryManager
from src.agent.context_manager import ContextManager
from src.safety import SafetyManager
from src.session import SessionManager, ContextRestorer
from src.utils import setup_logger, LogConfig
from src.utils.logger import generate_trace_id

# ── 模型目录 ──
PROJECT_ROOT = Path(__file__).parent
MODELS_DIR = PROJECT_ROOT / "models"

# 模型清单：(本地子目录, ModelScope model_id)
MODELS = [
    ("bge-m3", "BAAI/bge-m3"),
    ("bge-reranker-v2-m3", "BAAI/bge-reranker-v2-m3"),
    ("Llama-Guard-3-1B", "LLM-Research/Llama-Guard-3-1B"),
]

# ── 路径与限制常量 ──
SESSIONS_DIR = PROJECT_ROOT / "data" / "sessions"
MAX_INPUT_LENGTH = 10000


def ensure_models(console: Console) -> None:
    """检查本地模型是否存在，缺失则通过 ModelScope 自动下载"""
    from modelscope.hub.snapshot_download import snapshot_download

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    missing = []
    for local_name, ms_id in MODELS:
        local_path = MODELS_DIR / local_name
        if not local_path.exists() or not any(local_path.iterdir()):
            missing.append((local_name, ms_id))

    if not missing:
        console.print("[green]所有模型已在本地就绪[/green]")
        return

    for local_name, ms_id in missing:
        console.print(f"[yellow]模型 {local_name} 未找到，正在从 ModelScope 下载...[/yellow]")
        local_path = MODELS_DIR / local_name
        try:
            snapshot_download(ms_id, local_dir=str(local_path))
            console.print(f"[green]  {local_name} 下载完成[/green]")
        except Exception as e:
            console.print(f"[red]  {local_name} 下载失败: {e}[/red]")
            console.print(f"[yellow]  请检查网络连接或手动下载到 {local_path}[/yellow]")

    console.print("[green]所有模型下载完成[/green]")


def _validate_ingest_path(path_str: str) -> bool:
    """校验导入路径合法性：必须存在、在项目目录内、非系统敏感目录"""
    target = Path(path_str).resolve()

    if not target.exists():
        return False

    # 限制在项目目录内
    try:
        target.relative_to(PROJECT_ROOT)
    except ValueError:
        return False

    # 拒绝系统敏感目录
    forbidden_prefixes = [
        Path("/etc"), Path("/sys"), Path("/proc"), Path("/dev"),
        Path("C:\\Windows"), Path("C:\\System"),
    ]
    for prefix in forbidden_prefixes:
        try:
            target.relative_to(prefix)
            return False
        except ValueError:
            continue

    return True


def _print_recent_messages(console: Console, session_mgr: SessionManager, n: int) -> None:
    """打印最近 N 条消息"""
    recent = session_mgr.get_recent_messages(n)
    if not recent:
        console.print("[dim]（暂无历史消息）[/dim]")
        return

    session = session_mgr.current_session
    total = session.message_count if session else 0
    if total > n:
        console.print(f"[dim]（显示最近 {n} 条，共 {total} 条。输入 /history 查看全部）[/dim]")

    for msg in recent:
        if msg.role == "user":
            console.print(f"[bold blue]你:[/bold blue] {msg.content}")
        else:
            console.print(f"[bold green]风堇:[/bold green] {msg.content}")
    console.print("")


def _handle_command(cmd: str, args: str, console: Console,
                    session_mgr: SessionManager, context_restorer: ContextRestorer,
                    agent: Agent, memory_manager: MemoryManager,
                    rag_service, safety_engine,
                    max_turns: int) -> bool:
    """处理会话命令。返回 True 表示继续循环，False 表示退出。"""

    if cmd == "/quit":
        session_mgr.flush()
        agent.cleanup()
        if memory_manager:
            memory_manager.cleanup()
        rag_service.cleanup()
        safety_engine.cleanup()
        from loguru import logger
        logger.complete()
        console.print("[yellow]再见！[/yellow]")
        return False

    elif cmd == "/new":
        session_mgr.flush()
        session = session_mgr.create_session()
        agent.clear_history()
        console.print(f"[green]新会话已创建: {session.title}[/green]")
        console.print("[bold green]风堇:[/bold green] 灰宝~今天想聊什么呢？\n")
        return True

    elif cmd == "/list":
        sessions = session_mgr.list_sessions()
        if not sessions:
            console.print("[dim]暂无历史会话[/dim]")
            return True

        table = Table(title="会话列表")
        table.add_column("#", style="cyan", width=4)
        table.add_column("标题", style="white")
        table.add_column("消息数", style="yellow", width=6)
        table.add_column("最后活跃", style="green")

        current_id = session_mgr.get_current_session_id()
        for i, s in enumerate(sessions, 1):
            title = s["title"]
            if s["session_id"] == current_id:
                title = f"[bold]{title}（当前）[/bold]"
            table.add_row(
                str(i),
                title,
                str(s["message_count"]),
                s["updated_at"].strftime("%Y-%m-%d %H:%M"),
            )
        console.print(table)
        return True

    elif cmd == "/switch":
        if not args:
            console.print("[red]用法: /switch <编号>[/red]")
            return True

        sessions = session_mgr.list_sessions()
        try:
            idx = int(args) - 1
            if idx < 0 or idx >= len(sessions):
                raise ValueError
        except ValueError:
            console.print("[red]无效编号，请用 /list 查看会话列表[/red]")
            return True

        target = sessions[idx]
        session_mgr.flush()
        session = session_mgr.load_session(target["session_id"])
        if not session:
            console.print("[red]加载会话失败[/red]")
            return True

        # 恢复 Agent 对话历史（裁剪到滑动窗口限制）
        agent.messages = context_restorer.restore_llm_context(session)

        console.print(f"[green]已加载会话: {session.title}[/green]")
        _print_recent_messages(console, session_mgr, n=max_turns)
        return True

    elif cmd == "/history":
        if not session_mgr.current_session:
            console.print("[dim]暂无会话[/dim]")
            return True

        session = session_mgr.current_session
        console.print(f"[dim]=== {session.title}（共 {session.message_count} 条）===[/dim]")
        for msg in session.messages:
            if msg.role == "user":
                console.print(f"[bold blue]你:[/bold blue] {msg.content}")
            else:
                console.print(f"[bold green]风堇:[/bold green] {msg.content}")
        console.print("[dim]=== 结束 ===[/dim]\n")
        return True

    elif cmd == "/rename":
        if not args:
            console.print("[red]用法: /rename <新标题>[/red]")
            return True
        sid = session_mgr.get_current_session_id()
        if not sid:
            console.print("[dim]暂无会话[/dim]")
            return True
        session_mgr.rename_session(sid, args)
        console.print(f"[green]已重命名: {args}[/green]")
        return True

    elif cmd == "/delete":
        if not args:
            console.print("[red]用法: /delete <编号>[/red]")
            return True

        sessions = session_mgr.list_sessions()
        try:
            idx = int(args) - 1
            if idx < 0 or idx >= len(sessions):
                raise ValueError
        except ValueError:
            console.print("[red]无效编号，请用 /list 查看会话列表[/red]")
            return True

        target = sessions[idx]
        confirm = console.input(
            f"[yellow]确定删除会话「{target['title']}」？(y/n): [/yellow]"
        ).strip().lower()
        if confirm != "y":
            console.print("[dim]已取消[/dim]")
            return True

        deleted = session_mgr.delete_session(target["session_id"])
        if deleted:
            console.print(f"[green]已删除会话: {target['title']}[/green]")
        else:
            console.print(f"[yellow]会话「{target['title']}」已不存在（可能已被删除）[/yellow]")

        # 如果删的是当前会话，清空 Agent
        if target["session_id"] == session_mgr.get_current_session_id():
            agent.clear_history()
            console.print("[dim]当前会话已清空，请用 /new 创建新会话[/dim]")
        return True

    # 未知命令
    console.print(f"[red]未知命令: {cmd}[/red]")
    return True


def main():
    """主函数"""
    console = Console()

    # 初始化日志系统
    log_config = LogConfig(log_level="INFO", json_format=False)
    setup_logger(log_config)

    # 检查并下载模型（需要网络，完成后切离线）
    ensure_models(console)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    console.print("[dim]正在加载模型和初始化组件，请稍候...[/dim]")

    # 加载配置
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    config = Config.load(str(config_path))

    rag_config_path = PROJECT_ROOT / "config" / "rag.yaml"
    rag_config = RAGSettings.load(str(rag_config_path))

    # 加载上下文管理配置
    context_config_path = PROJECT_ROOT / "config" / "context.yaml"
    context_settings = ContextSettings.load(str(context_config_path))

    # 初始化记忆系统（可选：环境变量缺失时优雅降级，不阻塞启动）
    memory_config_path = PROJECT_ROOT / "config" / "memory.yaml"
    memory_settings = MemorySettings.load(str(memory_config_path))
    try:
        memory_manager = MemoryManager(memory_settings.memory)
    except Exception as e:
        get_logger("main").warning("记忆系统加载失败（环境变量未设？），记忆功能将不可用: {}", e)
        console.print("[yellow]⚠ 记忆系统暂不可用（环境变量缺失或配置错误），对话将无长期记忆[/yellow]")
        memory_manager = None

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

    # 创建 RAG 服务（纯功能层）并装配到 Agent
    rag_service = RAGService(rag_config, llm_client=agent.client)
    rag_server = RAGMCPServer(rag_service)
    agent.register_mcp(rag_server)

    # 初始化安全护栏（P0 规则引擎 + P1 Llama Guard）
    safety_engine = SafetyManager(
        config_path=str(PROJECT_ROOT / "config" / "safety.yaml")
    )

    # 初始化会话管理
    session_mgr = SessionManager(str(SESSIONS_DIR))
    context_restorer = ContextRestorer(
        context_manager=context_manager,
        memory_retriever=memory_manager,
    )

    # 尝试恢复上次会话
    sessions = session_mgr.list_sessions()
    if sessions:
        last = sessions[0]
        session = session_mgr.load_session(last["session_id"])
        if session:
            agent.messages = context_restorer.restore_llm_context(session)
            console.print(f"[dim]已恢复上次会话: {session.title}[/dim]")

    # 显示欢迎信息
    current_title = session_mgr.current_session.title if session_mgr.current_session else "无"
    console.print(Panel.fit(
        f"[bold green]{config.agent.name}[/bold green]\n"
        f"模型: {config.model}\n"
        f"已装配 MCP: [bold cyan]rag[/bold cyan]\n"
        f"上下文管理: [bold cyan]{'启用' if context_settings.context.memory.enabled else '未启用'}[/bold cyan]\n"
        f"当前会话: [bold white]{current_title}[/bold white]\n"
        "\n"
        "输入 [bold red]/quit[/bold red] 退出\n"
        "输入 [bold yellow]/new[/bold yellow] 新建会话\n"
        "输入 [bold yellow]/list[/bold yellow] 查看会话列表\n"
        "输入 [bold yellow]/switch <编号>[/bold yellow] 切换会话\n"
        "输入 [bold yellow]/history[/bold yellow] 查看当前会话全部历史\n"
        "输入 [bold yellow]/rename <标题>[/bold yellow] 重命名当前会话\n"
        "输入 [bold yellow]/delete <编号>[/bold yellow] 删除会话\n"
        "输入 [bold magenta]/ingest <文件路径>[/bold magenta] 导入文档\n"
        "输入 [bold magenta]/ingest_dir <目录路径>[/bold magenta] 批量导入知识库\n"
        "输入 [bold white]/stats[/bold white] 查看知识库状态\n"
        "输入 [bold cyan]/tools[/bold cyan] 查看可用工具\n"
        "输入 [bold cyan]/skills[/bold cyan] 查看已装配技能\n"
        "输入 [bold cyan]/mcp[/bold cyan] 查看 MCP 服务器",
        title="[Agent 启动]"
    ))

    # 对话循环
    user_input = ""
    while True:
        try:
            # 初始化异常兜底变量（命令处理器抛异常时异常处理器引用它们）
            trace_id = ""
            msg_count_before = 0
            session_count_before = 0

            try:
                user_input = console.input("[bold blue]你:[/bold blue] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[yellow]再见！[/yellow]")
                break

            # 会话管理命令
            if user_input.startswith("/"):
                parts = user_input.split(maxsplit=1)
                cmd = parts[0].lower()
                args = parts[1].strip() if len(parts) > 1 else ""

                # 会话管理命令
                if cmd in ("/quit", "/new", "/list", "/switch", "/history", "/rename", "/delete"):
                    should_continue = _handle_command(
                        cmd, args, console, session_mgr, context_restorer,
                        agent, memory_manager, rag_service, safety_engine,
                        max_turns=context_settings.context.sliding_window.max_turns,
                    )
                    if not should_continue:
                        break
                    continue

                # RAG/工具命令
                if cmd == "/clear":
                    session_mgr.flush()
                    agent.clear_history()
                    session_mgr.create_session()
                    console.print("[green]对话历史已清空，新会话已创建[/green]")
                    continue

                elif cmd == "/ingest_dir" and args:
                    try:
                        if not _validate_ingest_path(args):
                            console.print("[red]无效路径：请提供项目目录下的合法路径[/red]")
                            continue
                        result = rag_service.ingest_directory(args, recursive=True)
                        console.print(f"[green]成功导入 {result['document_count']} 个文档，共 {result['total_chunks']} 个文本块[/green]")
                    except Exception as e:
                        console.print(f"[red]导入失败: {e}[/red]")
                    continue

                elif cmd == "/ingest" and args:
                    try:
                        if not _validate_ingest_path(args):
                            console.print("[red]无效路径：请提供项目目录下的合法路径[/red]")
                            continue
                        result = rag_service.ingest_document(args)
                        console.print(f"[green]成功导入文档，生成 {result['chunk_count']} 个文本块[/green]")
                    except Exception as e:
                        console.print(f"[red]导入失败: {e}[/red]")
                    continue

                elif cmd == "/stats":
                    stats = rag_service.get_stats()
                    table = Table(title="知识库状态")
                    table.add_column("属性", style="cyan")
                    table.add_column("值", style="green")
                    for key, value in stats.items():
                        table.add_row(str(key), str(value))
                    console.print(table)
                    continue

                elif cmd == "/tools":
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

                elif cmd == "/skills":
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

                elif cmd == "/mcp":
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

                else:
                    console.print(f"[red]未知命令: {cmd}[/red]")
                    continue

            elif not user_input:
                continue

            # 输入长度检查
            if len(user_input) > MAX_INPUT_LENGTH:
                console.print(f"[red]输入过长（{len(user_input)}字符），请限制在 {MAX_INPUT_LENGTH} 字符以内[/red]")
                continue

            # 确保有当前会话
            if not session_mgr.current_session:
                session_mgr.create_session()

            trace_id = generate_trace_id()

            # 安全护栏检查（核心1 §2.5：被拦截消息仍记录到会话，但不送入AI）
            result = safety_engine.check(user_input, trace_id=trace_id)
            if result.blocked:
                session_mgr.append_message("user", user_input)
                msg = result.user_message or "小伊卡发现了一些不太对劲的内容呢~请换个话题吧！"
                console.print(f"[yellow]小伊卡：{msg}[/yellow]")
                session_mgr.append_message("assistant", f"[小伊卡拦截] {msg}")
                session_mgr.flush()
                continue

            # 发送消息（chat() 内部已流式输出）
            console.print("[bold green]风堇:[/bold green]")
            # 记录回滚基准（用于 ToolCalling 同步 + 异常兜底；在 append 之前记录）
            msg_count_before = len(agent.messages)
            session_count_before = len(session_mgr.current_session.messages) if session_mgr.current_session else 0
            session_mgr.append_message("user", user_input)
            if result.action.value == "comfort":
                reply = agent.chat(user_input, safety_context=result.comfort_prompt, trace_id=trace_id)
            else:
                reply = agent.chat(user_input, trace_id=trace_id)

            # 同步 Tool calling 中间消息到会话（保证会话恢复时上下文完整）
            for msg in agent.messages[msg_count_before:]:
                if msg.get("role") == "tool":
                    session_mgr.append_message("tool", msg["content"])

            # 记录助手回复到会话（与用户消息配对写入）
            session_mgr.append_message("assistant", reply)
            session_mgr.flush()

            console.print(f"[dim]对话轮数: {agent.history_count}[/dim]\n")

        except KeyboardInterrupt:
            # 回滚本轮新增消息，避免孤儿 user 持久化
            from src.agent.message_builder import rollback_last_user
            rollback_last_user(session_mgr, user_input, agent.messages, msg_count_before, session_count_before)
            session_mgr.flush()
            agent.cleanup()
            if memory_manager:
                memory_manager.cleanup()
            rag_service.cleanup()
            safety_engine.cleanup()
            from loguru import logger
            logger.complete()
            console.print("\n[yellow]再见！[/yellow]")
            break
        except Exception as e:
            from src.utils import get_logger
            _log = get_logger(trace_id)
            _log.opt(exception=True).error("对话循环异常 [input={}]: {}", user_input[:50], e)
            # 回滚本轮所有消息：复用共享回滚函数
            from src.agent.message_builder import rollback_last_user
            rollback_last_user(session_mgr, user_input, agent.messages, msg_count_before, session_count_before)
            session_mgr.flush()
            console.print("[red]对话处理出错，请重试。详情见日志文件。[/red]")


if __name__ == "__main__":
    main()
