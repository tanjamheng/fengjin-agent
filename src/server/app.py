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

# ── 知识库源目录（翁法罗斯世界观资料）──
_KNOWLEDGE_SRC_DIR = "数据侧_风堇资料"

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

    # ── 知识库预构建检查 ──
    # .knowledge_built 标记 = 构建完成证明。标记缺失 → 加构建步骤。
    # 不在此处验证 chroma DB（会与后续 RAG 初始化冲突 chromadb 连接池）。
    # 实际构建代码（预处理 + 系统加载）内部会检查 count，已构建则跳过并补标记。
    knowledge_src = project_root / _KNOWLEDGE_SRC_DIR
    if knowledge_src.is_dir():
        _built_marker = project_root / "data" / "chroma" / ".knowledge_built"
        if not _built_marker.exists():
            steps.append("knowledge_build")

    return steps


def _cleanup_partial_knowledge(chroma_dir: Path) -> None:
    """删除残缺的 ChromaDB 知识库目录（上次构建未完成）"""
    import shutil
    try:
        shutil.rmtree(chroma_dir, ignore_errors=True)
        log.info("已清理残缺知识库: {}", chroma_dir)
    except Exception as e:
        log.warning("清理残缺知识库失败（将尝试继续构建）: {}", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时加载共享单例，关闭时释放资源。

    Launcher 模式 (FENGJIN_LAUNCHER_MODE=1):
    向 stdout 逐行发送 JSON 进度消息，供 Electron 主进程解析。
    """
    log.info("正在加载应用级单例（模型检查 / 配置 / 安全 / 记忆 / RAG / 工具）...")
    mind_manager = None
    rag_service = None
    try:
        _project_root = Path(__file__).resolve().parent.parent.parent

        # ── 0. Python logging 抑制 + stderr 重定向（绝对第一）──
        # 不碰 os.dup2——uvicorn 依赖原始 stderr fd，重定向会导致进程立即退出
        try:
            import logging as _logging
            _logging.getLogger().setLevel(_logging.ERROR)
            _logging.captureWarnings(True)
            import sys as _sys
            _log_dir = _project_root / "logs"
            _log_dir.mkdir(exist_ok=True)
            _sys.stderr = open(str(_log_dir / "stderr.log"), "a", encoding="utf-8")
        except Exception:
            pass

        # ── 1. 扫描 + 发送预处理计划（尽早让前端看到步骤列表）──
        preprocess_steps = _scan_preprocess_plan(_project_root)
        emit_preprocess_plan(preprocess_steps)

        # ── 2. GPU 优化 + 预算预计算 ──
        try:
            import torch
            os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
        except ImportError:
            pass
        except Exception as _e:
            log.debug("GPU 优化跳过: {}", _e)

        from ..utils.gpu_budget import init_budget, check_system_memory
        mem_status = check_system_memory()
        if mem_status == "refuse":
            log.error("系统可用内存不足 1GB，拒绝加载本地模型（云端对话仍可用）")
        elif mem_status == "degraded":
            log.warning("系统可用内存不足 2GB，仅加载核心模型")
        init_budget(mem_status=mem_status)

        # ── 3. 模型检查：下载 + FP16 量化 ──
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

        # ── 4. 知识库预构建（属于预处理阶段，无看门狗限制）──
        if "knowledge_build" in preprocess_steps:
            emit_progress("knowledge_build", "start")
            _rag_build = None
            try:
                from ..rag.rag_service import RAGService as _RAGService
                _rag_build = _RAGService(
                    config=RAGSettings.load(str(_project_root / "config" / "rag.yaml")),
                    llm_client=None,
                )
                _rag_build.initialize()
                _knowledge_src = _project_root / _KNOWLEDGE_SRC_DIR
                if _knowledge_src.is_dir() and _rag_build.indexer is not None:
                    if _rag_build.indexer.count() == 0:
                        log.info("预处理阶段：知识库为空，自动导入: {}", _knowledge_src)
                        # ① 加载 + 切分，统计总 chunk 数
                        _docs = _rag_build.loader.load_directory_recursive(str(_knowledge_src))
                        _doc_chunks: list[tuple] = []
                        _total_chunks = 0
                        for _doc in _docs:
                            _chunks = _rag_build.splitter.split_document(_doc)
                            _doc_chunks.append((_doc, _chunks))
                            _total_chunks += len(_chunks)
                        # _docs 是生成器，已耗尽；用 _doc_chunks 长度代表文档数
                        _doc_count = len(_doc_chunks)
                        # ② 子批次嵌入（每 8 chunk 一批）
                        if _total_chunks > 0:
                            _BATCH = 8
                            _chunks_done = 0
                            _pending: list = []
                            for _doc, _chunks in _doc_chunks:
                                for _chunk in _chunks:
                                    _pending.append(_chunk)
                                    if len(_pending) >= _BATCH:
                                        _rag_build.indexer.add(_pending)
                                        _chunks_done += len(_pending)
                                        _pending.clear()
                                        emit_progress("knowledge_build", "progress",
                                                      percent=round(_chunks_done / _total_chunks * 100))
                            if _pending:
                                _rag_build.indexer.add(_pending)
                                _chunks_done += len(_pending)
                                emit_progress("knowledge_build", "progress",
                                              percent=round(_chunks_done / _total_chunks * 100))
                        log.info("预处理阶段：知识库构建完成: {} 文档, {} chunks",
                                 _doc_count, _total_chunks)
                        if _total_chunks > 0:
                            (_project_root / "data" / "chroma" / ".knowledge_built").write_text("ok")
                    else:
                        log.info("预处理阶段：知识库非空 ({} 条)，跳过构建",
                                 _rag_build.indexer.count())
                        _marker = _project_root / "data" / "chroma" / ".knowledge_built"
                        if not _marker.exists():
                            _marker.write_text("ok")
                            log.info("已补建知识库标记")
                emit_progress("knowledge_build", "done")
            except Exception as e:
                log.warning("预处理阶段：知识库构建失败（将在系统加载阶段重试）: {}", e)
                emit_warn("knowledge_build", f"知识库构建失败: {e}")
            finally:
                # 无论成功失败，释放预处理阶段占用的 GPU 资源
                if _rag_build is not None:
                    try:
                        _rag_build.cleanup()
                    except Exception:
                        pass

        # ── 系统加载前预算重算（预处理已释放 GPU，以干净显存为基点）──
        from ..utils.gpu_budget import recalc_budget
        recalc_budget()

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

        # ── 4. 心智配置 ──
        emit_progress("engine_init:memory", "start")
        memory_config = None
        mind_enabled = os.environ.get("MIND_ENABLED", "false").lower() == "true"
        try:
            from ..memory.config import MemorySettings
            memory_config = MemorySettings.load(
                str(_project_root / "config" / "memory.yaml")
            ).memory
        except Exception as e:
            log.warning("心智记忆配置加载失败: {}", e)
        emit_progress("engine_init:memory", "done")

        # ── 5. 情绪引擎 ──
        emit_progress("engine_init:mood", "start")
        try:
            from ..mood.engine import MoodSettings, MoodEngine
            mood_config = MoodSettings.load(
                str(_project_root / "config" / "mood.yaml")
            )
            mood_engine = MoodEngine(mood_config, data_dir=_project_root / "data")
            app.state.mood_config = mood_config
            app.state.mood_engine = mood_engine
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
            bond_tracker = BondTracker(bond_config, data_dir=_project_root / "data")
            app.state.bond_config = bond_config
            app.state.bond_tracker = bond_tracker
            log.info("羁绊引擎已加载")
        except Exception as e:
            log.warning("羁绊引擎加载失败: {}", e)
            app.state.bond_config = None
            app.state.bond_tracker = None
        emit_progress("engine_init:bond", "done")

        # 记忆、情绪、羁绊收口到应用级心智协调器。
        if memory_config is not None and app.state.mood_engine and app.state.bond_tracker:
            from ..mind import MindManager, MindSettings
            mind_config = MindSettings.load(
                str(_project_root / "config" / "mind.yaml")
            ).mind
            mind_manager = MindManager(
                mind_config,
                memory_config,
                app.state.mood_engine,
                app.state.bond_tracker,
                max_context_tokens=app.state.context_config.sliding_window.max_tokens,
                enabled=mind_enabled,
            )
            app.state.mind_manager = mind_manager
            app.state.memory_manager = mind_manager.memory_manager
        else:
            app.state.mind_manager = None
            app.state.memory_manager = None

        # ── 7. 角色漂移检测配置 ──
        # PersonaDriftGuard 是连接级对象，实际编码模型由正式 RAG 服务长期持有并共享。
        # 此处仅加载配置，不能为启动校验临时加载 bge-m3 后又立即释放。
        emit_progress("engine_init:persona", "start")
        try:
            from ..persona.drift_guard import PersonaSettings
            _model_path = str(_project_root / "models" / "bge-m3")
            persona_config = PersonaSettings.load(
                str(_project_root / "config" / "persona.yaml")
            )
            app.state.persona_config = persona_config
            app.state.persona_model_path = _model_path
            app.state.persona_guard = None
            log.info("角色漂移检测配置已加载（bge-m3 由 RAG 常驻共享）")
        except Exception as e:
            log.warning("角色漂移检测加载失败: {}", e)
            app.state.persona_config = None
            app.state.persona_model_path = ""
            app.state.persona_guard = None
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

            # ── 知识库验证 + 兜底补建（内嵌于 rag 步骤，不做独立步骤）──
            if rag_service is not None:
                try:
                    if hasattr(rag_service, 'indexer') and rag_service.indexer is not None:
                        _kb_count = rag_service.indexer.count()
                        if _kb_count == 0:
                            # 预处理阶段未构建，此处兜底导入
                            _kb_dir = _project_root / _KNOWLEDGE_SRC_DIR
                            if _kb_dir.is_dir():
                                log.info("知识库为空，rag 步骤兜底导入: {}", _kb_dir)
                                _docs = rag_service.loader.load_directory_recursive(str(_kb_dir))
                                _doc_chunks: list[tuple] = []
                                _total_chunks = 0
                                for _doc in _docs:
                                    _chunks = rag_service.splitter.split_document(_doc)
                                    _doc_chunks.append((_doc, _chunks))
                                    _total_chunks += len(_chunks)
                                _chunks_done = 0
                                if _total_chunks > 0:
                                    _BATCH = 8
                                    _pending: list = []
                                    for _doc, _chunks in _doc_chunks:
                                        for _chunk in _chunks:
                                            _pending.append(_chunk)
                                            if len(_pending) >= _BATCH:
                                                rag_service.indexer.add(_pending)
                                                _chunks_done += len(_pending)
                                                _pending.clear()
                                                emit_progress("engine_init:rag", "progress",
                                                              percent=round(_chunks_done / _total_chunks * 100))
                                    if _pending:
                                        rag_service.indexer.add(_pending)
                                        _chunks_done += len(_pending)
                                        emit_progress("engine_init:rag", "progress",
                                                      percent=round(_chunks_done / _total_chunks * 100))
                                log.info("知识库兜底构建完成: {} 文档, {} chunks",
                                         len(_doc_chunks), _total_chunks)
                                if _total_chunks > 0:
                                    (_project_root / "data" / "chroma" / ".knowledge_built").write_text("ok")
                            else:
                                log.warning("知识库目录不存在: {}", _kb_dir)
                        else:
                            log.info("知识库已就绪: {} 条记录", _kb_count)
                            # 确保标记存在（可能从旧版升级而来）
                            _marker = _project_root / "data" / "chroma" / ".knowledge_built"
                            if not _marker.exists():
                                _marker.write_text("ok")
                except Exception as e:
                    log.warning("知识库验证失败（不影响对话）: {}", e)
        except Exception as e:
            log.warning("RAG 知识库加载失败，WS 路径无知识检索: {}", e)
            app.state.tool_definitions = None
            app.state.tool_registry = None
            app.state.mcp_manager = None
            app.state.rag_service = None
        emit_progress("engine_init:rag", "done")

        log.info("应用级单例加载完成")
        active_port = int(os.environ.get("FENGJIN_ACTIVE_WS_PORT", "8765"))
        emit_ready(active_port)

    except Exception as e:
        log.opt(exception=True).error("应用级单例加载失败，服务无法启动: {}", e)
        emit_fatal("init_failed", str(e))
        # 部分初始化回滚（红线19）
        # MindManager 统一拥有记忆、情绪、羁绊及其后台工作线程。
        if mind_manager:
            try:
                mind_manager.cleanup()
            except Exception as ce:
                log.warning("mind_manager cleanup 异常: {}", ce)
        for attr in ("persona_guard",):
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
    if mind_manager:
        try:
            mind_manager.cleanup()
        except Exception as e:
            log.warning("MindManager 清理异常: {}", e)
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
