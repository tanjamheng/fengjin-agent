"""FastAPI 应用工厂"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI

from ..config import Config, ContextSettings
from ..safety import SafetyManager
from ..utils.logger import get_logger

log = get_logger("server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时加载共享单例，关闭时释放资源

    SafetyManager 内含 Llama Guard GPU 模型，加载约 13 秒，
    提升为应用级单例后只加载一次（而非每个连接重载），符合资源红线。
    """
    log.info("正在加载应用级单例（配置 / OpenAI / 安全模型 / 记忆）...")
    memory_manager = None
    try:
        config = Config.load()
        app.state.config = config
        app.state.client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=120.0,
            max_retries=3,
        )
        app.state.context_config = ContextSettings.load().context
        app.state.safety = SafetyManager()   # Llama Guard 在此加载（仅一次）

        # 记忆系统（可选：环境变量缺失时优雅降级，不阻塞服务启动）
        try:
            from ..memory import MemorySettings
            memory_config = MemorySettings.load().memory
            from ..memory.manager import MemoryManager
            memory_manager = MemoryManager(memory_config)
            log.info("记忆系统已加载")
        except Exception as e:
            log.warning("记忆系统加载失败（环境变量未设？），WS 路径无记忆增强: {}", e)

        app.state.memory_manager = memory_manager
        log.info("应用级单例加载完成")
    except Exception as e:
        log.opt(exception=True).error("应用级单例加载失败，服务无法启动: {}", e)
        raise

    yield

    # 关闭：释放资源（对称释放，client 也需 close）
    try:
        await app.state.client.close()
    except Exception as e:
        log.warning("OpenAI client 关闭异常: {}", e)
    if memory_manager:
        try:
            memory_manager.cleanup()
        except Exception as e:
            log.warning("MemoryManager 清理异常: {}", e)
    app.state.safety.cleanup()
    log.info("应用资源已释放")


def create_app() -> FastAPI:
    app = FastAPI(title="风堇AI Agent", version="1.0.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],           # 本地桌面应用，允许所有来源
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from ..ws.connection import router
    app.include_router(router)

    return app
