"""FastAPI 应用工厂"""

import os
import hashlib
from contextlib import asynccontextmanager
import inspect
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI

from ..config import Config, ContextSettings, RAGSettings
from ..safety import SafetyManager
from ..utils.logger import get_logger
from ..utils.ws_token import get_or_create_ws_token
from ..utils.progress import (
    emit_preprocess_plan, emit_progress, emit_warn, emit_fatal, emit_ready,
)

log = get_logger("server")

_MISSING_API_KEY_PLACEHOLDER = "launcher-missing-api-key"

# ── 模型清单（与 src/utils/models.py 保持同步）──
_MODEL_SPECS = [
    ("bge-m3", True),               # (目录名, 必需)
    ("bge-reranker-v2-m3", True),
    ("Llama-Guard-3-1B", False),    # 仅 FENGJIN_GUARD_MODEL_ENABLED=true 时启用
]


async def _cleanup_resource(name: str, resource) -> None:
    """清理单个资源，兼容 async close() 与 sync cleanup()。"""
    try:
        if hasattr(resource, "close"):
            result = resource.close()
            if inspect.isawaitable(result):
                await result
        elif hasattr(resource, "cleanup"):
            resource.cleanup()
    except Exception as e:
        log.warning("{} 清理异常: {}", name, e)


def _scan_preprocess_plan(project_root: Path) -> list[str]:
    """扫描模型文件状态 + 知识库状态，生成预处理步骤清单。

    每个模型检查 .state 文件：
    - 无目录 / .state 异常 → download + quantize
    - .state=fp32 → 只需 quantize
    - .state=fp16 → 跳过

    Llama Guard 仅在 FENGJIN_GUARD_MODEL_ENABLED=true 时检查。
    """
    steps = []
    models_dir = project_root / "models"
    guard_enabled = os.environ.get("FENGJIN_GUARD_MODEL_ENABLED", "false").lower() == "true"

    for dir_name, _required in _MODEL_SPECS:
        # Llama Guard 条件跳过
        if dir_name == "Llama-Guard-3-1B" and not guard_enabled:
            continue

        target = models_dir / dir_name
        state_file = target / ".state"

        if not target.exists():
            # 目录不存在 → 需要下载 + 量化
            steps.append(f"model_download:{dir_name}")
            steps.append(f"model_quantize:{dir_name}")
            continue

        state = None
        if state_file.exists():
            state = state_file.read_text().strip()

        if state == "fp16":
            continue  # 已完成
        elif state == "fp32":
            steps.append(f"model_quantize:{dir_name}")
        else:
            # 无 .state 或状态异常 → 需要下载 + 量化
            steps.append(f"model_download:{dir_name}")
            steps.append(f"model_quantize:{dir_name}")

    return steps


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时加载共享单例，关闭时释放资源。

    Launcher 模式 (FENGJIN_LAUNCHER_MODE=1):
    向 stdout 逐行发送 JSON 进度消息，供 Electron 主进程解析。
    """
    log.info("正在加载应用级单例（模型检查 / 配置 / 安全 / 记忆 / RAG / 工具）...")
    memory_manager = None
    rag_service = None
    try:
        _project_root = Path(__file__).resolve().parent.parent.parent

        # ── 0. 扫描 + 发送预处理计划 ──
        preprocess_steps = _scan_preprocess_plan(_project_root)
        emit_preprocess_plan(preprocess_steps)

        # ── 1. 模型检查：下载 + FP16 量化 ──
        from ..utils.models import ensure_models as _ensure_models

        def _model_progress(step_id: str, status: str, percent: int | None = None):
            """ensure_models 的进度回调 → 发射 JSON 到 stdout"""
            if status == "failed":
                emit_warn(step_id, f"{step_id} 失败，已跳过")
                return
            emit_progress(step_id, status, percent)

        _all_ok = _ensure_models(
            msg=lambda text: log.info(text),
            progress_callback=_model_progress,
        )
        if not _all_ok:
            log.warning("部分模型加载失败，对话功能可能受限")
            emit_warn("model_check", "部分模型加载失败，对话功能可能受限。查看日志了解详情。")

        # ── 2. 配置 + 客户端 ──
        config = Config.load(str(_project_root / "config" / "config.yaml"))
        app.state.config = config
        app.state.client = AsyncOpenAI(
            api_key=config.api_key or _MISSING_API_KEY_PLACEHOLDER,
            base_url=config.base_url,
            timeout=120.0,
            max_retries=3,
        )
        app.state.context_config = ContextSettings.load(
            str(_project_root / "config" / "context.yaml")
        ).context

        # ── 3. 安全护栏 ──
        emit_progress("engine_init:safety", "start")
        app.state.safety = SafetyManager(
            config_path=str(_project_root / "config" / "safety.yaml")
        )
        emit_progress("engine_init:safety", "done")

        # ── 4. 记忆系统 ──
        emit_progress("engine_init:memory", "start")
        try:
            memory_enabled = os.environ.get("MEMORY_ENABLED", "false").lower() == "true"
            if memory_enabled:
                from ..memory.config import MemorySettings
                memory_config = MemorySettings.load(
                    str(_project_root / "config" / "memory.yaml")
                ).memory
                from ..memory.manager import MemoryManager
                memory_manager = MemoryManager(memory_config)
                log.info("记忆系统已加载")
            else:
                log.info("记忆系统已禁用 (MEMORY_ENABLED=false)")
        except Exception as e:
            log.warning("记忆系统加载失败（环境变量未设？），WS 路径无记忆增强: {}", e)
        app.state.memory_manager = memory_manager
        emit_progress("engine_init:memory", "done")

        # ── 5. 情绪引擎 ──
        emit_progress("engine_init:mood", "start")
        try:
            from ..mood.engine import MoodSettings, MoodEngine
            mood_config = MoodSettings.load(
                str(_project_root / "config" / "mood.yaml")
            )
            MoodEngine(mood_config, data_dir=_project_root / "data")
            app.state.mood_config = mood_config
            app.state.mood_engine = None
            log.info("情绪引擎已加载")
        except Exception as e:
            log.warning("情绪引擎加载失败: {}", e)
            app.state.mood_config = None
            app.state.mood_engine = None
        emit_progress("engine_init:mood", "done")

        # ── 6. 羁绊追踪 ──
        emit_progress("engine_init:bond", "start")
        try:
            from ..bond.tracker import BondSettings, BondTracker
            bond_config = BondSettings.load(
                str(_project_root / "config" / "bond.yaml")
            )
            BondTracker(bond_config, data_dir=_project_root / "data")
            app.state.bond_config = bond_config
            app.state.bond_tracker = None
            log.info("羁绊引擎已加载")
        except Exception as e:
            log.warning("羁绊引擎加载失败: {}", e)
            app.state.bond_config = None
            app.state.bond_tracker = None
        emit_progress("engine_init:bond", "done")

        # ── 7. 角色漂移检测 ──
        emit_progress("engine_init:persona", "start")
        _persona_emb_acquired = False
        try:
            from ..persona.drift_guard import PersonaSettings, PersonaDriftGuard
            from ..rag import embedding_registry as _emb_reg
            _model_path = str(_project_root / "models" / "bge-m3")
            _emb = _emb_reg.acquire(_model_path, "cpu")
            _persona_emb_acquired = True
            persona_config = PersonaSettings.load(
                str(_project_root / "config" / "persona.yaml")
            )
            persona_guard = PersonaDriftGuard(_emb, persona_config)
            if persona_guard.anchor_count >= 3:
                anchor_count = persona_guard.anchor_count
                persona_guard.cleanup()
                _emb_reg.release()
                _persona_emb_acquired = False
                app.state.persona_config = persona_config
                app.state.persona_model_path = _model_path
                app.state.persona_guard = None
                log.info("角色漂移检测已加载: {} 条锚点", anchor_count)
            else:
                log.warning("角色锚点不足（<3），漂移检测不可用")
                persona_guard.cleanup()
                _emb_reg.release()
                _persona_emb_acquired = False
                app.state.persona_config = None
                app.state.persona_model_path = ""
                app.state.persona_guard = None
        except Exception as e:
            log.warning("角色漂移检测加载失败: {}", e)
            app.state.persona_config = None
            app.state.persona_model_path = ""
            app.state.persona_guard = None
            try:
                if _persona_emb_acquired:
                    _emb_reg.release()
            except Exception:
                pass
        emit_progress("engine_init:persona", "done")

        # ── 8. RAG 知识库 + 工具注册 ──
        emit_progress("engine_init:rag", "start")
        try:
            from ..rag.rag_service import RAGService
            from ..agent.tool_registry import ToolRegistry
            from ..agent.mcp_manager import MCPManager
            from ..mcp_servers.rag_server import RAGMCPServer

            rag_service = RAGService(
                config=RAGSettings.load(str(_project_root / "config" / "rag.yaml")),
                llm_client=None,
            )

            tool_registry = ToolRegistry()
            mcp_manager = MCPManager()
            rag_mcp = RAGMCPServer(rag_service)
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
        emit_progress("engine_init:rag", "done")

        # ── 9. 知识库自动构建（仅首次，需 RAGService 已初始化）──
        emit_progress("engine_init:knowledge", "start")
        _knowledge_docs = 0
        _knowledge_chunks = 0
        if rag_service is not None:
            try:
                # 检查 ChromaDB collection 是否为空
                if hasattr(rag_service, 'indexer') and rag_service.indexer is not None:
                    if rag_service.indexer.count() == 0:
                        knowledge_dir = _project_root / "数据侧_风堇资料"
                        if knowledge_dir.is_dir():
                            log.info("知识库为空，自动导入: {}", knowledge_dir)
                            result = rag_service.ingest_directory(
                                str(knowledge_dir), recursive=True
                            )
                            _knowledge_docs = result.get("document_count", 0)
                            _knowledge_chunks = result.get("total_chunks", 0)
                            log.info("知识库构建完成: {} 文档, {} chunks",
                                     _knowledge_docs, _knowledge_chunks)
                        else:
                            log.warning("知识库目录不存在: {}", knowledge_dir)
            except Exception as e:
                log.warning("知识库自动构建失败（不影响对话）: {}", e)
                emit_warn("engine_init:knowledge", f"知识库构建失败: {e}")
        emit_progress("engine_init:knowledge", "done")

        log.info("应用级单例加载完成")
        emit_ready()

    except Exception as e:
        log.opt(exception=True).error("应用级单例加载失败，服务无法启动: {}", e)
        emit_fatal("init_failed", str(e))
        # 部分初始化回滚（红线19）
        for attr in ("persona_guard", "bond_tracker", "mood_engine"):
            obj = getattr(app.state, attr, None)
            if obj and hasattr(obj, "cleanup"):
                try:
                    obj.cleanup()
                except Exception as ce:
                    log.warning("{} cleanup 异常: {}", attr, ce)
        if rag_service:
            try:
                rag_service.cleanup()
            except Exception as ce:
                log.warning("rag_service cleanup 异常: {}", ce)
        mcp_mgr = getattr(app.state, "mcp_manager", None)
        if mcp_mgr:
            try:
                mcp_mgr.cleanup_all()
            except Exception as ce:
                log.warning("mcp_manager cleanup 异常: {}", ce)
        tool_reg = getattr(app.state, "tool_registry", None)
        if tool_reg:
            tool_reg.clear()
        if memory_manager:
            try:
                memory_manager.cleanup()
            except Exception as ce:
                log.warning("memory_manager cleanup 异常: {}", ce)
        try:
            app.state.safety.cleanup()
        except Exception as ce:
            log.warning("safety cleanup 异常: {}", ce)
        try:
            await app.state.client.close()
        except Exception as ce:
            log.warning("client close 异常: {}", ce)
        raise

    yield

    # 关闭：释放资源
    await _cleanup_resource("OpenAI client", app.state.client)
    for idx, resource in enumerate(getattr(app.state, "_retired_resources", []), start=1):
        await _cleanup_resource(f"retired_resource[{idx}]", resource)
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
    if getattr(app.state, "persona_guard", None):
        try:
            app.state.persona_guard.cleanup()
        except Exception as e:
            log.warning("PersonaDriftGuard 清理异常: {}", e)
    app.state.safety.cleanup()
    log.info("应用资源已释放")
    from loguru import logger
    logger.complete()


def create_app() -> FastAPI:
    app = FastAPI(title="风堇AI Agent", version="1.0.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        token = get_or_create_ws_token(log)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
        return {"status": "ready", "token_hash": token_hash}

    from ..ws.connection import router
    app.include_router(router)

    return app
