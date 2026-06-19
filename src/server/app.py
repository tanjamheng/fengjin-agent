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
    log.info("正在加载应用级单例（配置 / OpenAI / 安全模型）...")
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
