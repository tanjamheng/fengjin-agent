"""运行时配置热更新 — 写 .env → os.environ → 重建客户端"""

import os
import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from openai import AsyncOpenAI

from ..utils.logger import get_logger
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
        "MIND_API_KEY": ("mind", "api_key"),
        "MIND_BASE_URL": ("mind", "base_url"),
        "MIND_MODEL": ("mind", "model"),
    }

    @staticmethod
    def update_env_file(main: dict, mind: dict, mind_enabled: bool | None = None) -> bool:
        """原子写入 .env：先写 .tmp 再 os.replace

        main/mind 中的 null 值表示该字段不更新，保持原值。
        """
        env_path = _PROJECT_ROOT / ".env"
        if not env_path.exists():
            log.error(".env 文件不存在")
            return False

        # 构建 env_key → new_value 的更新映射
        updates: dict[str, str] = {}
        for section_name, section in [("main", main), ("mind", mind)]:
            for env_key, (sec, field) in ConfigManager._KEY_MAP.items():
                if sec != section_name:
                    continue
                val = section.get(field)
                if val is None:
                    continue  # null = 不改
                updates[env_key] = str(val).strip()

        if mind_enabled is not None:
            updates["MIND_ENABLED"] = "true" if mind_enabled else "false"

        if not updates:
            log.info("配置无变更，跳过写入")
            return True

        initial_count = len(updates)

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
            replaced = initial_count - len(updates)
            appended = len(updates)
            log.info(".env 配置已更新 ({} 替换, {} 新增)", replaced, appended)
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
    def apply_to_os_environ(main: dict, mind: dict, mind_enabled: bool | None = None) -> None:
        """立即更新 os.environ（不写 .env，仅运行时生效）"""
        for section_name, section in [("main", main), ("mind", mind)]:
            for env_key, (sec, field) in ConfigManager._KEY_MAP.items():
                if sec != section_name:
                    continue
                val = section.get(field)
                if val is None:
                    continue
                os.environ[env_key] = str(val).strip()
                log.debug("os.environ[{}] = {}", env_key, "***" if "KEY" in env_key else os.environ[env_key])
        if mind_enabled is not None:
            os.environ["MIND_ENABLED"] = "true" if mind_enabled else "false"

    @staticmethod
    async def rebuild_clients(app, main: dict, mind: dict, mind_enabled: bool) -> None:
        """重建主模型客户端，并在稳定的 MindManager 内重配心智服务。

        规则：
        - 主模型配置有变更 → 重建 AsyncOpenAI 客户端
        - 心智配置/开关有变更 → 在稳定的 MindManager 内重建两个后台服务
        - 旧资源先停止并关闭，再启动新代次，防止任务串代与连接泄漏
        """
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
                if old_client and app.state.client is not old_client:
                    ConfigManager._retire_resource(app, old_client)

        manager = getattr(app.state, "mind_manager", None)
        if manager:
            manager.reconfigure(mind_enabled)
            app.state.memory_manager = manager.memory_manager

    @staticmethod
    def _build_config_from_env() -> Config:
        """从 os.environ 读取当前配置，构建 Config 对象（用于重建客户端）

        注意：不调用 _reload_dotenv()——调用方（rebuild_clients）的调用者
        （connection.py update_config）已先 apply_to_os_environ 将新值写入 os.environ。
        如果在此处 reload，load_dotenv(override=True) 会用旧 .env 覆盖新 os.environ。
        """
        config_path = _PROJECT_ROOT / "config" / "config.yaml"
        return Config.load(str(config_path))

    @staticmethod
    def get_current_config() -> dict:
        """从 .env 文件读取当前配置（重新加载），返回完整 API Key
        前端 password 输入框负责视觉遮蔽，后端不再脱敏。
        """
        _reload_dotenv()  # 确保读取最新 .env，而非可能过时的 os.environ

        main_ak = os.environ.get("FENGJIN_API_KEY", "")
        mind_ak = os.environ.get("MIND_API_KEY", "")

        # 心智总开关默认关闭，只有显式开启时才运行记忆、情绪和羁绊。
        mind_enabled_str = os.environ.get("MIND_ENABLED", "")
        if mind_enabled_str:
            mind_enabled = mind_enabled_str.lower() == "true"
        else:
            mind_enabled = False

        return {
            "main": {
                "api_key": main_ak,
                "base_url": os.environ.get("FENGJIN_BASE_URL", ""),
                "model": os.environ.get("FENGJIN_MODEL", ""),
            },
            "mind": {
                "api_key": mind_ak,
                "base_url": os.environ.get("MIND_BASE_URL", ""),
                "model": os.environ.get("MIND_MODEL", ""),
            },
            "mind_enabled": mind_enabled,
        }

    @staticmethod
    def _retire_resource(app, resource) -> None:
        """延迟清理被热更新替换的资源，避免其他 WS 连接持有已关闭对象。"""
        retired = getattr(app.state, "_retired_resources", None)
        if retired is None:
            retired = []
            app.state._retired_resources = retired
        retired.append(resource)

    @staticmethod
    def register_connection(app) -> None:
        app.state._active_ws_connections = getattr(app.state, "_active_ws_connections", 0) + 1

    @staticmethod
    async def unregister_connection(app) -> None:
        app.state._active_ws_connections = max(
            0, getattr(app.state, "_active_ws_connections", 0) - 1
        )
        if app.state._active_ws_connections == 0:
            await ConfigManager.cleanup_retired_resources(app)

    @staticmethod
    async def cleanup_retired_resources(app) -> None:
        retired = getattr(app.state, "_retired_resources", [])
        if not retired:
            return
        app.state._retired_resources = []
        for idx, resource in enumerate(retired, start=1):
            try:
                if hasattr(resource, "close"):
                    result = resource.close()
                    if hasattr(result, "__await__"):
                        await result
                elif hasattr(resource, "cleanup"):
                    resource.cleanup()
            except Exception as e:
                log.warning("清理 retired_resource[{}] 异常: {}", idx, e)


def _reload_dotenv():
    """重新加载 .env 到 os.environ（幂等）"""
    load_dotenv(_PROJECT_ROOT / ".env", override=True)
