"""FastAPI 应用工厂"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI

from ..config import Config, ContextSettings, RAGSettings
from ..safety import SafetyManager
from ..utils.logger import get_logger

log = get_logger("server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时加载共享单例，关闭时释放资源

    SafetyManager 内含 Llama Guard GPU 模型，加载约 13 秒，
    提升为应用级单例后只加载一次（而非每个连接重载），符合资源红线。
    """
    log.info("正在加载应用级单例（模型检查 / 配置 / 安全 / 记忆 / RAG / 工具）...")
    memory_manager = None
    rag_service = None
    try:
        # 使用基于 __file__ 的绝对路径，与 CLI 路径保持一致
        _project_root = Path(__file__).resolve().parent.parent.parent

        # ── 0. 模型检查：下载 + FP16 量化（在加载任何模型之前）──
        from ..utils.models import ensure_models as _ensure_models
        _ensure_models(msg=lambda text: log.info(text))

        config = Config.load(str(_project_root / "config" / "config.yaml"))
        app.state.config = config
        app.state.client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=120.0,
            max_retries=3,
        )
        app.state.context_config = ContextSettings.load(
            str(_project_root / "config" / "context.yaml")
        ).context
        app.state.safety = SafetyManager(
            config_path=str(_project_root / "config" / "safety.yaml")
        )   # Llama Guard 在此加载（仅一次）

        # 记忆系统（可选：环境变量缺失时优雅降级，不阻塞服务启动）
        try:
            from ..memory.config import MemorySettings
            memory_config = MemorySettings.load(
                str(_project_root / "config" / "memory.yaml")
            ).memory
            from ..memory.manager import MemoryManager
            memory_manager = MemoryManager(memory_config)
            log.info("记忆系统已加载")
        except Exception as e:
            log.warning("记忆系统加载失败（环境变量未设？），WS 路径无记忆增强: {}", e)

        app.state.memory_manager = memory_manager

        # 情绪状态机（无上游依赖，无 GPU，总是成功）
        try:
            from ..mood.engine import MoodSettings, MoodEngine
            mood_config = MoodSettings.load(
                str(_project_root / "config" / "mood.yaml")
            )
            mood_engine = MoodEngine(mood_config, data_dir=_project_root / "data")
            app.state.mood_engine = mood_engine
            log.info("情绪引擎已加载")
        except Exception as e:
            log.warning("情绪引擎加载失败: {}", e)
            app.state.mood_engine = None

        # 羁绊状态机（对齐情绪引擎，无上游依赖，无 GPU，总是成功）
        try:
            from ..bond.tracker import BondSettings, BondTracker
            bond_config = BondSettings.load(
                str(_project_root / "config" / "bond.yaml")
            )
            bond_tracker = BondTracker(bond_config, data_dir=_project_root / "data")
            app.state.bond_tracker = bond_tracker
            log.info("羁绊引擎已加载")
        except Exception as e:
            log.warning("羁绊引擎加载失败: {}", e)
            app.state.bond_tracker = None

        # RAG 知识库 + 工具注册表（可选：知识库为空时仍可正常对话）
        try:
            from ..rag.rag_service import RAGService
            from ..agent.tool_registry import ToolRegistry
            from ..agent.mcp_manager import MCPManager
            from ..mcp_servers.rag_server import RAGMCPServer

            rag_service = RAGService(
                config=RAGSettings.load(str(_project_root / "config" / "rag.yaml")),
                llm_client=None,  # WS 路径不传同步 client，RAG 仅用检索能力
            )

            tool_registry = ToolRegistry()
            mcp_manager = MCPManager()
            rag_mcp = RAGMCPServer(rag_service)
            # register() 触发 rag_mcp.initialize() → rag_service.initialize()，避免重复 init
            mcp_manager.register(rag_mcp)
            tool_registry.register_mcp_server(rag_mcp)

            app.state.tool_definitions = tool_registry.get_all_definitions()
            app.state.tool_registry = tool_registry
            app.state.mcp_manager = mcp_manager
            app.state.rag_service = rag_service
            log.info("RAG 知识库 + Tool 注册表已加载（{} 个工具）", len(app.state.tool_definitions))
        except Exception as e:
            log.warning("RAG 知识库加载失败，WS 路径无知识检索: {}", e)
            app.state.tool_definitions = None
            app.state.tool_registry = None
            app.state.mcp_manager = None
            app.state.rag_service = None

        log.info("应用级单例加载完成")
    except Exception as e:
        log.opt(exception=True).error("应用级单例加载失败，服务无法启动: {}", e)
        raise

    yield

    # 关闭：释放资源（对称释放，清理顺序：MCP→RAG→Tool→Memory→Mood→Bond→Safety）
    try:
        await app.state.client.close()
    except Exception as e:
        log.warning("OpenAI client 关闭异常: {}", e)
    if getattr(app.state, "mcp_manager", None):
        try:
            app.state.mcp_manager.cleanup_all()
        except Exception as e:
            log.warning("MCPManager 清理异常: {}", e)
    if rag_service:
        try:
            rag_service.cleanup()
        except Exception as e:
            log.warning("RAGService 清理异常: {}", e)
    tool_registry = getattr(app.state, "tool_registry", None)
    if tool_registry:
        tool_registry.clear()
    if memory_manager:
        try:
            memory_manager.cleanup()
        except Exception as e:
            log.warning("MemoryManager 清理异常: {}", e)
    if getattr(app.state, "mood_engine", None):
        try:
            app.state.mood_engine.cleanup()
        except Exception as e:
            log.warning("MoodEngine 清理异常: {}", e)
    if getattr(app.state, "bond_tracker", None):
        try:
            app.state.bond_tracker.cleanup()
        except Exception as e:
            log.warning("BondTracker 清理异常: {}", e)
    app.state.safety.cleanup()
    log.info("应用资源已释放")
    from loguru import logger
    logger.complete()  # 等待异步日志队列排空（enqueue=True 的 handler）


def create_app() -> FastAPI:
    app = FastAPI(title="风堇AI Agent", version="1.0.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],           # 本地桌面应用，允许所有来源
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        """健康检查 — lifespan 完成后才可达，表示所有模型已加载完毕"""
        return {"status": "ready"}

    from ..ws.connection import router
    app.include_router(router)

    return app
