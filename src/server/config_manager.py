"""运行时配置热更新 — 写 .env → os.environ → 重建客户端"""

import os
import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from openai import AsyncOpenAI

from ..utils.logger import get_logger
from ..memory.config import MemorySettings
from ..config import Config

log = get_logger("config_manager")

# 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class ConfigManager:
    """写 .env 文件 + 更新 os.environ + 重建 app.state 客户端

    所有方法均为纯函数/静态方法，无内部状态。
    """

    # .env key → 字段名映射
    _KEY_MAP = {
        "FENGJIN_API_KEY": ("main", "api_key"),
        "FENGJIN_BASE_URL": ("main", "base_url"),
        "FENGJIN_MODEL": ("main", "model"),
        "MEMO_API_KEY": ("memory", "api_key"),
        "MEMO_BASE_URL": ("memory", "base_url"),
        "MEMO_MODEL": ("memory", "model"),
    }

    @staticmethod
    def update_env_file(main: dict, memory: dict) -> bool:
        """原子写入 .env：先写 .tmp 再 os.replace

        main/memory 中的 null 值表示该字段不更新，保持原值。
        """
        env_path = _PROJECT_ROOT / ".env"
        if not env_path.exists():
            log.error(".env 文件不存在")
            return False

        # 构建 env_key → new_value 的更新映射
        updates: dict[str, str] = {}
        for section_name, section in [("main", main), ("memory", memory)]:
            for env_key, (sec, field) in ConfigManager._KEY_MAP.items():
                if sec != section_name:
                    continue
                val = section.get(field)
                if val is None:
                    continue  # null = 不改
                updates[env_key] = str(val).strip()

        if not updates:
            log.info("配置无变更，跳过写入")
            return True

        # 逐行读取、替换、写入临时文件
        tmp_path = env_path.with_suffix(".env.tmp")
        try:
            with open(env_path, "r", encoding="utf-8") as fin:
                lines = fin.readlines()

            with open(tmp_path, "w", encoding="utf-8") as fout:
                for line in lines:
                    stripped = line.strip()
                    # 保留空行和注释
                    if not stripped or stripped.startswith("#"):
                        fout.write(line)
                        continue
                    # 匹配 KEY=VALUE 行
                    m = re.match(r"^(\w+)\s*=\s*(.*)", stripped)
                    if m and m.group(1) in updates:
                        fout.write(f"{m.group(1)}={updates[m.group(1)]}\n")
                        del updates[m.group(1)]  # 标记已处理
                    else:
                        fout.write(line)

                # 追加本次要更新但 .env 中不存在的 key（首次配置场景）
                for key, value in updates.items():
                    fout.write(f"{key}={value}\n")

            # 原子替换
            os.replace(tmp_path, env_path)
            log.info(".env 配置已更新 ({} 项)", len(updates) + len(updates))
            return True

        except Exception as e:
            log.opt(exception=True).error("写入 .env 失败: {}", e)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            return False

    @staticmethod
    def apply_to_os_environ(main: dict, memory: dict) -> None:
        """立即更新 os.environ（不写 .env，仅运行时生效）"""
        for section_name, section in [("main", main), ("memory", memory)]:
            for env_key, (sec, field) in ConfigManager._KEY_MAP.items():
                if sec != section_name:
                    continue
                val = section.get(field)
                if val is None:
                    continue
                os.environ[env_key] = str(val).strip()
                log.debug("os.environ[{}] = {}", env_key, "***" if "KEY" in env_key else os.environ[env_key])

    @staticmethod
    async def rebuild_clients(app, main: dict, memory: dict, memory_enabled: bool) -> None:
        """重建 app.state 上的客户端和记忆管理器

        规则：
        - 主模型配置有变更 → 重建 AsyncOpenAI 客户端
        - 记忆配置/开关有变更 → 重建 MemoryManager
        - 旧客户端先 close 再替换，防止连接泄漏
        """
        from ..agent.context_manager import ContextManager

        # ── 主模型客户端 ──
        need_rebuild_main = any(
            main.get(k) is not None for k in ("api_key", "base_url", "model")
        )
        if need_rebuild_main:
            old_client = getattr(app.state, "client", None)
            try:
                config = ConfigManager._build_config_from_env()
                app.state.client = AsyncOpenAI(
                    api_key=config.api_key,
                    base_url=config.base_url,
                    timeout=120.0,
                    max_retries=3,
                )
                log.info("主模型客户端已重建")
            except Exception as e:
                log.opt(exception=True).error("重建主模型客户端失败: {}", e)
                # 回滚：保留旧客户端
                if old_client:
                    app.state.client = old_client
                raise
            finally:
                if old_client:
                    try:
                        await old_client.close()
                    except Exception as e:
                        log.warning("关闭旧客户端异常: {}", e)

        # ── 记忆管理器 ──
        need_rebuild_memory = memory_enabled != (getattr(app.state, "memory_manager", None) is not None)
        need_rebuild_memory = need_rebuild_memory or any(
            memory.get(k) is not None for k in ("api_key", "base_url", "model")
        )

        if need_rebuild_memory:
            old_mgr = getattr(app.state, "memory_manager", None)
            try:
                if memory_enabled:
                    _reload_dotenv()
                    mem_settings = MemorySettings.load(
                        str(_PROJECT_ROOT / "config" / "memory.yaml")
                    ).memory
                    from ..memory.manager import MemoryManager
                    app.state.memory_manager = MemoryManager(mem_settings)
                    log.info("记忆管理器已重建（启用）")
                else:
                    app.state.memory_manager = None
                    log.info("记忆管理器已关闭")
            except Exception as e:
                log.opt(exception=True).error("重建记忆管理器失败: {}", e)
                app.state.memory_manager = old_mgr  # 回滚
            finally:
                if old_mgr and old_mgr is not app.state.memory_manager:
                    try:
                        old_mgr.cleanup()
                    except Exception as e:
                        log.warning("清理旧记忆管理器异常: {}", e)

        # ── 如果记忆管理器变了，也需要重建 context_manager 的引用 ──
        if need_rebuild_memory:
            # context_manager 的 memory_retriever 引用需更新（仅当记忆管理器变化时）
            pass  # WS 连接中 context_mgr 是 per-connection，暂不需要全局更新

    @staticmethod
    def _build_config_from_env() -> Config:
        """从 os.environ 读取当前配置，构建 Config 对象（用于重建客户端）"""
        _reload_dotenv()
        config_path = _PROJECT_ROOT / "config" / "config.yaml"
        return Config.load(str(config_path))

    @staticmethod
    def get_current_config() -> dict:
        """从 os.environ 读取当前配置，脱敏后返回"""
        def mask_key(k: str) -> str:
            if not k or len(k) <= 6:
                return "****"
            return "****" + k[-4:]

        main_ak = os.environ.get("FENGJIN_API_KEY", "")
        memo_ak = os.environ.get("MEMO_API_KEY", "")

        return {
            "main": {
                "api_key": mask_key(main_ak),
                "base_url": os.environ.get("FENGJIN_BASE_URL", ""),
                "model": os.environ.get("FENGJIN_MODEL", ""),
            },
            "memory": {
                "api_key": mask_key(memo_ak),
                "base_url": os.environ.get("MEMO_BASE_URL", ""),
                "model": os.environ.get("MEMO_MODEL", ""),
            },
            "memory_enabled": os.environ.get("MEMO_API_KEY", "") != "",
        }


def _reload_dotenv():
    """重新加载 .env 到 os.environ（幂等）"""
    load_dotenv(_PROJECT_ROOT / ".env", override=True)
