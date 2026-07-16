"""WebSocket 端点 — 消息路由 + 报文映射（瘦传输层）

只负责：协议消息路由、报文收发、会话 CRUD 路由、对话事件→报文映射。
对话业务逻辑（安全 / 上下文 / LLM / Tool / 落盘）在 Agent.chat()（core.py）。
"""

import json
import os
import asyncio
import secrets
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..session import SessionManager
from ..config import Config
from ..agent.context_manager import ContextManager
from ..agent.core import Agent, BlockedError, MAX_INPUT_LENGTH
from ..utils.logger import get_logger, generate_trace_id
from ..utils.ws_token import get_or_create_ws_token

router = APIRouter()
log = get_logger("ws")

SERVER_PING_INTERVAL = 25    # 秒，服务端主动发 ping（asyncio，不受浏览器节流影响）
HEARTBEAT_TIMEOUT = 60       # 秒，超时无 pong 则断连
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not _is_ws_request_allowed(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    log.info("WebSocket 客户端已连接")
    from ..server.config_manager import ConfigManager
    ConfigManager.register_connection(websocket.app)

    try:
        async with _get_config_lock(websocket.app):
            (
                config, client, safety, session_mgr, mind_manager, context_mgr,
                tool_registry, mood_engine, bond_tracker, persona_guard,
                persona_embedding_acquired,
            ) = _setup_connection_resources(websocket)
    except Exception:
        await ConfigManager.unregister_connection(websocket.app)
        raise

    event_loop = asyncio.get_running_loop()

    def _notify_mind_failure() -> None:
        async def _send():
            try:
                await websocket.send_json({
                    "type": "mind_warning",
                    "message": "心智模型好像出了点问题呢。",
                })
            except Exception as exc:
                log.debug("心智提示发送失败（连接可能已关闭）: {}", exc)
        try:
            event_loop.call_soon_threadsafe(asyncio.create_task, _send())
        except RuntimeError:
            log.debug("心智提示跳过：事件循环已关闭")

    # Agent 构造和首包发送仍可能因早期断连失败；此阶段也必须对称释放资源。
    agent = None
    try:
        agent = Agent(
            config=config,
            session_mgr=session_mgr,
            safety=safety,
            client=client,
            context_manager=context_mgr,
            mind_manager=mind_manager,
            on_mind_warning=_notify_mind_failure,
            tool_registry=tool_registry,
            mood_engine=mood_engine,
            bond_tracker=bond_tracker,
            persona_guard=persona_guard,
        )
        async with _get_config_lock(websocket.app):
            # 构造期间配置可能已完成一次事务；注册前重新绑定已提交版本。
            agent.client = websocket.app.state.client
            agent.config = websocket.app.state.config
            ConfigManager.register_agent(websocket.app, agent)
        # 不预先创建会话——等用户发送第一条消息时才创建
        await websocket.send_json({"type": "connected", "session_id": ""})
    except Exception:
        if agent is not None:
            ConfigManager.unregister_agent(websocket.app, agent)
        if persona_guard:
            try:
                persona_guard.cleanup()
            except Exception as cleanup_exc:
                log.warning("早期断连时 PersonaDriftGuard 清理异常: {}", cleanup_exc)
        if persona_embedding_acquired:
            try:
                from ..rag import embedding_registry as _emb_reg
                _emb_reg.release()
            except Exception as release_exc:
                log.warning("早期断连时 embedding release 异常: {}", release_exc)
        await ConfigManager.unregister_connection(websocket.app)
        raise

    # 流任务状态
    current_stream: Optional[asyncio.Task] = None

    # 心跳：服务端主动发 ping（asyncio，不受浏览器 JS 定时器节流影响）
    last_pong = asyncio.get_event_loop().time()

    async def _heartbeat_sender():
        """每 SERVER_PING_INTERVAL 秒发送 ping，直至连接关闭"""
        while True:
            await asyncio.sleep(SERVER_PING_INTERVAL)
            try:
                await websocket.send_json({"type": "ping"})
            except Exception:
                break  # 连接已关闭，退出

    async def _heartbeat_checker():
        """每 10s 检查一次，超过 HEARTBEAT_TIMEOUT 无 pong 则断连"""
        while True:
            await asyncio.sleep(10)
            elapsed = asyncio.get_event_loop().time() - last_pong
            if elapsed > HEARTBEAT_TIMEOUT:
                log.warning("心跳超时（{:.0f}s），关闭连接", elapsed)
                try:
                    await websocket.close()
                except Exception as e:
                    log.debug("心跳关闭连接异常: {}", e)
                break

    heartbeat_sender = asyncio.create_task(_heartbeat_sender())
    heartbeat_checker = asyncio.create_task(_heartbeat_checker())

    try:
        async for raw in websocket.iter_text():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("收到非法 JSON: {}", raw[:100])
                continue

            msg_type = data.get("type")

            # ── pong ──（客户端响应服务端 ping）
            if msg_type == "pong":
                last_pong = asyncio.get_event_loop().time()

            # ── user_msg ──
            elif msg_type == "user_msg":
                if current_stream and not current_stream.done():
                    agent.cancel()
                    try:
                        await asyncio.wait_for(current_stream, timeout=30)
                    except asyncio.TimeoutError:
                        current_stream.cancel()
                        try:
                            await current_stream
                        except (asyncio.CancelledError, Exception):
                            pass
                    except asyncio.CancelledError:
                        pass
                    except BlockedError:
                        pass
                    except Exception as e:
                        log.opt(exception=True).error("旧流异常收尾: {}", e)

                # 切换/创建会话（目标会话不存在则报错，不继续）
                if not _ensure_session(session_mgr, data.get("session_id", ""),
                                       agent=agent, persona_guard=persona_guard):
                    await websocket.send_json({"type": "error", "message": "会话不存在"})
                    continue

                current_stream = asyncio.create_task(
                    _handle_user_msg(websocket, data, agent, session_mgr)
                )

            # ── cancel ──
            elif msg_type == "cancel":
                agent.cancel()
                if current_stream and not current_stream.done():
                    try:
                        await asyncio.wait_for(current_stream, timeout=5)
                    except asyncio.TimeoutError:
                        current_stream.cancel()
                        try:
                            await current_stream
                        except (asyncio.CancelledError, Exception):
                            pass
                    except asyncio.CancelledError:
                        pass
                    except BlockedError:
                        pass
                    except Exception as e:
                        log.opt(exception=True).error("取消旧流异常: {}", e)
                # _handle_user_msg 已完成并发送了对应的 end/blocked/error 报文，不重复发送

            # ── list_sessions ──
            elif msg_type == "list_sessions":
                await websocket.send_json({
                    "type": "session_list",
                    "sessions": _format_session_list(session_mgr.list_sessions()),
                })

            # ── load_session ──
            elif msg_type == "load_session":
                # 若有流式任务正在运行，先取消等待（防止并发写入错乱）
                if current_stream and not current_stream.done():
                    agent.cancel()
                    try:
                        await asyncio.wait_for(current_stream, timeout=30)
                    except asyncio.TimeoutError:
                        current_stream.cancel()
                        try:
                            await current_stream
                        except (asyncio.CancelledError, Exception):
                            pass
                    except (asyncio.CancelledError, BlockedError):
                        pass
                    except Exception as e:
                        log.opt(exception=True).error("load_session 取消旧流异常: {}", e)
                loaded = session_mgr.load_session(data.get("session_id", ""))
                if loaded:
                    # 会话切换 → 清理漂移保护状态（对齐 CLI /switch 行为）
                    agent._pending_anchor = None
                    if persona_guard:
                        persona_guard.reset_state()
                    if agent.mood_engine:
                        agent.mood_engine.reset_state()
                    if agent.bond_tracker:
                        agent.bond_tracker.reset_state()
                    await websocket.send_json({
                        "type": "session_loaded",
                        "session_id": loaded.session_id,
                        "title": loaded.title,
                        "messages": [
                            {
                                "role": m.role,
                                "content": m.display_content,
                                "timestamp": m.timestamp.isoformat(),
                            }
                            for m in loaded.messages
                        ],
                    })
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": "会话不存在",
                    })

            # ── delete_session ──
            elif msg_type == "delete_session":
                await _cancel_active_stream(agent, current_stream, "delete_session")
                target_id = data.get("session_id", "")
                is_current_session = bool(target_id and target_id == session_mgr.get_current_session_id())
                deleted = session_mgr.delete_session(target_id)
                if not deleted:
                    await websocket.send_json({
                        "type": "error",
                        "message": "会话不存在，删除失败",
                    })
                    continue
                # 若删除的是当前会话，清理漂移保护状态（对齐 CLI /delete 行为）
                if is_current_session:
                    agent._pending_anchor = None
                    if persona_guard:
                        persona_guard.reset_state()
                    if agent.mood_engine:
                        agent.mood_engine.reset_state()
                    if agent.bond_tracker:
                        agent.bond_tracker.reset_state()
                await websocket.send_json({
                    "type": "session_deleted",
                    "session_id": target_id,
                })

            # ── rename_session ──
            elif msg_type == "rename_session":
                await _cancel_active_stream(agent, current_stream, "rename_session")
                target_id = data.get("session_id", "")
                new_title = (data.get("title", "") or "").strip()
                if not target_id or not new_title:
                    await websocket.send_json({
                        "type": "error",
                        "message": "会话ID或标题不能为空",
                    })
                else:
                    ok = session_mgr.rename_session(target_id, new_title)
                    if ok:
                        await websocket.send_json({
                            "type": "session_renamed",
                            "session_id": target_id,
                            "title": new_title,
                        })
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "message": "会话不存在，重命名失败",
                        })

            # ── get_config ──
            elif msg_type == "get_config":
                from ..server.config_manager import ConfigManager
                async with _get_config_lock(websocket.app):
                    cfg = ConfigManager.get_current_config()
                await websocket.send_json({
                    "type": "current_config",
                    **cfg,
                })

            # ── update_config ──
            elif msg_type == "update_config":
                from ..server.config_manager import ConfigManager

                main_cfg = data.get("main", {})
                mind_cfg = data.get("mind", {})
                mind_enabled = bool(data.get("mind_enabled", False))

                # 心智模型缺少配置时允许保存，由后端记录警告并关闭心智旁路。
                errors = _validate_config(main_cfg, "主模型")

                if errors:
                    await websocket.send_json({
                        "type": "config_updated",
                        "success": False,
                        "errors": errors,
                    })
                    continue

                success, update_errors = await _apply_config_update(
                    websocket.app, main_cfg, mind_cfg, mind_enabled,
                    agent.on_mind_warning,
                )
                client = websocket.app.state.client
                agent.client = client
                await websocket.send_json({
                    "type": "config_updated",
                    "success": success,
                    **({"errors": update_errors} if update_errors else {}),
                })

    except WebSocketDisconnect:
        log.info("客户端主动断开")
    except Exception as e:
        log.opt(exception=True).error("WebSocket 异常: {}", e)
    finally:
        for task in (heartbeat_sender, heartbeat_checker):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if current_stream and not current_stream.done():
            agent.cancel()  # 协作式旗标：让 Agent.chat() 优雅停止
            try:
                # 给流任务 5 秒时间自然结束（保存部分文字）
                await asyncio.wait_for(current_stream, timeout=5)
            except asyncio.TimeoutError:
                # 超时则强制取消
                current_stream.cancel()
                try:
                    await current_stream
                except asyncio.CancelledError:
                    pass
            except Exception as e:
                # 流任务以其他异常结束
                log.warning("流任务异常结束: {}", e)

        session_mgr.flush()
        for name, obj in (("persona_guard", persona_guard),):
            if obj and hasattr(obj, "cleanup"):
                try:
                    obj.cleanup()
                except Exception as e:
                    log.warning("{} 连接级 cleanup 异常: {}", name, e)
        if persona_embedding_acquired:
            try:
                from ..rag import embedding_registry as _emb_reg
                _emb_reg.release()
            except Exception as e:
                log.warning("连接级 persona embedding release 异常: {}", e)
        ConfigManager.unregister_agent(websocket.app, agent)
        await ConfigManager.unregister_connection(websocket.app)
        log.info("WebSocket 连接关闭，会话已保存")


# ── 对话事件 → 报文映射 ──────────────────────────────────────


def _setup_connection_resources(websocket: WebSocket) -> tuple:
    """构造连接级资源；Persona acquire 在任何异常路径都对称释放。"""
    app_state = websocket.app.state
    config = app_state.config
    client = app_state.client
    safety = app_state.safety
    session_mgr = SessionManager(data_dir=str(_PROJECT_ROOT / "data" / "sessions"))
    mind_manager = getattr(app_state, "mind_manager", None)
    context_mgr = ContextManager(
        app_state.context_config,
        memory_retriever=mind_manager if mind_manager else None,
    )
    tool_registry = getattr(app_state, "tool_registry", None)
    mood_engine = mind_manager.mood_engine if mind_manager else None
    bond_tracker = mind_manager.bond_tracker if mind_manager else None
    persona_guard = None
    persona_embedding_acquired = False

    persona_config = getattr(app_state, "persona_config", None)
    persona_model_path = getattr(app_state, "persona_model_path", "")
    if persona_config is not None and persona_model_path:
        try:
            from ..persona.drift_guard import PersonaDriftGuard
            from ..rag import embedding_registry as embedding_registry
            from ..utils.helpers import resolve_device

            embedding = embedding_registry.acquire(
                persona_model_path, resolve_device("auto", "bge-m3")
            )
            persona_embedding_acquired = True
            persona_guard = PersonaDriftGuard(embedding, persona_config)
            if persona_guard.anchor_count < 3:
                persona_guard.cleanup()
                persona_guard = None
                embedding_registry.release()
                persona_embedding_acquired = False
        except Exception as exc:
            log.warning("连接级角色漂移检测创建失败: {}", exc)
            if persona_guard:
                try:
                    persona_guard.cleanup()
                except Exception as cleanup_exc:
                    log.warning("连接级 Persona 回滚异常: {}", cleanup_exc)
            persona_guard = None
            if persona_embedding_acquired:
                try:
                    embedding_registry.release()
                except Exception as release_exc:
                    log.warning("连接级 embedding 回滚异常: {}", release_exc)
                persona_embedding_acquired = False

    return (
        config, client, safety, session_mgr, mind_manager, context_mgr,
        tool_registry, mood_engine, bond_tracker, persona_guard,
        persona_embedding_acquired,
    )


def _get_config_lock(app) -> asyncio.Lock:
    """应用级配置事务锁，序列化所有 WS 的读取、重建与持久化。"""
    lock = getattr(app.state, "_config_update_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state._config_update_lock = lock
    return lock


async def _cancel_active_stream(agent: Agent, task: Optional[asyncio.Task],
                                operation: str) -> None:
    """会话写操作前结束当前生成，避免磁盘快照覆盖在途消息。"""
    if task is None or task.done():
        return
    agent.cancel()
    try:
        await asyncio.wait_for(task, timeout=30)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    except (asyncio.CancelledError, BlockedError):
        pass
    except Exception as exc:
        log.opt(exception=True).error("{} 取消旧流异常: {}", operation, exc)


async def _apply_config_update(app, main_cfg: dict, mind_cfg: dict,
                               mind_enabled: bool,
                               on_mind_failure=None) -> tuple[bool, list[str]]:
    from ..server.config_manager import ConfigManager

    env_keys = (
        "FENGJIN_API_KEY", "FENGJIN_BASE_URL", "FENGJIN_MODEL",
        "MIND_API_KEY", "MIND_BASE_URL", "MIND_MODEL", "MIND_ENABLED",
    )
    async with _get_config_lock(app):
        old_environ = {key: os.environ.get(key) for key in env_keys}
        old_client = getattr(app.state, "client", None)
        old_config = getattr(app.state, "config", None)
        agents = list(getattr(app.state, "_active_agents", ()))
        old_agent_refs = [(agent, agent.client, agent.config) for agent in agents]
        manager = getattr(app.state, "mind_manager", None)
        runtime_barrier = (
            manager.begin_config_update() if manager is not None else None
        )
        old_mind_generation = getattr(manager, "_generation", None)
        old_mind_runtime_version = (
            manager.model_runtime.current_version
            if manager is not None and getattr(manager, "model_runtime", None) is not None
            else None
        )

        async def _rollback_runtime() -> None:
            _restore_environ(old_environ)
            new_client = getattr(app.state, "client", None)
            app.state.client = old_client
            app.state.config = old_config
            for active_agent, agent_client, agent_config in old_agent_refs:
                active_agent.client = agent_client
                active_agent.config = agent_config
            retired = getattr(app.state, "_retired_resources", [])
            while old_client in retired:
                retired.remove(old_client)
            if new_client is not None and new_client is not old_client:
                try:
                    await new_client.close()
                except Exception as close_exc:
                    log.warning("回滚时关闭新主模型客户端失败: {}", close_exc)
            # 只有本次 rebuild 真正触碰过心智 generation，才重建旧代。
            if manager is not None and getattr(manager, "_generation", None) != old_mind_generation:
                old_enabled = (old_environ.get("MIND_ENABLED") or "false").lower() == "true"
                try:
                    manager.reconfigure_background(old_enabled, on_mind_failure)
                    app.state.memory_manager = manager.memory_manager
                except Exception as rollback_exc:
                    log.opt(exception=True).error("心智配置回滚失败: {}", rollback_exc)
            elif (
                manager is not None
                and old_mind_runtime_version is not None
                and getattr(manager, "model_runtime", None) is not None
                and manager.model_runtime.current_version != old_mind_runtime_version
            ):
                try:
                    await asyncio.to_thread(
                        manager.update_model_runtime, on_mind_failure
                    )
                except Exception as rollback_exc:
                    log.opt(exception=True).error("心智模型运行时回滚失败: {}", rollback_exc)

        try:
            ConfigManager.apply_to_os_environ(main_cfg, mind_cfg, mind_enabled)
            try:
                await ConfigManager.rebuild_clients(
                    app, main_cfg, mind_cfg, mind_enabled,
                    previous_environ=old_environ,
                    on_mind_failure=on_mind_failure,
                    defer_mind_reconfigure=True,
                )
            except Exception as exc:
                log.opt(exception=True).error("配置热更新失败，正在回滚: {}", exc)
                await _rollback_runtime()
                return False, ["配置热更新失败，已恢复原配置"]

            if not ConfigManager.update_env_file(main_cfg, mind_cfg, mind_enabled):
                await _rollback_runtime()
                return False, ["配置持久化失败，已恢复原配置"]
            old_enabled = (
                (old_environ.get("MIND_ENABLED") or "false").lower() == "true"
            )
            if manager is not None and old_enabled != mind_enabled:
                manager.reconfigure_background(mind_enabled, on_mind_failure)
                app.state.memory_manager = manager.memory_manager
            if getattr(app.state, "_active_chat_count", 0) == 0:
                await ConfigManager.cleanup_retired_resources(app)
            return True, []
        finally:
            if manager is not None:
                manager.end_config_update(runtime_barrier)


def _restore_environ(snapshot: dict[str, str | None]) -> None:
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

async def _handle_user_msg(
    websocket: WebSocket,
    data: dict,
    agent: Agent,
    session_mgr: SessionManager,
):
    """跟踪应用级在途对话，使热更新旧客户端在最后一轮结束后及时释放。"""
    app = websocket.app
    # 与配置事务串行完成快照；锁随即释放，不阻塞整轮生成。
    async with _get_config_lock(app):
        chat_client = agent.client
        chat_config = agent.config
        app.state._active_chat_count = getattr(app.state, "_active_chat_count", 0) + 1
    try:
        return await _handle_user_msg_impl(
            websocket, data, agent, session_mgr, chat_client, chat_config
        )
    finally:
        app.state._active_chat_count = max(
            0, getattr(app.state, "_active_chat_count", 0) - 1
        )
        if app.state._active_chat_count == 0:
            from ..server.config_manager import ConfigManager
            async with _get_config_lock(app):
                await ConfigManager.cleanup_retired_resources(app)


async def _handle_user_msg_impl(
    websocket: WebSocket,
    data: dict,
    agent: Agent,
    session_mgr: SessionManager,
    chat_client,
    chat_config,
):
    """消费 Agent.chat() 的 token，映射为 WS 报文"""
    user_content = data.get("content", "")

    # 输入校验
    if not isinstance(user_content, str) or not user_content.strip():
        await websocket.send_json({"type": "error", "message": "消息不能为空"})
        return
    if len(user_content) > MAX_INPUT_LENGTH:
        await websocket.send_json({"type": "error", "message": "消息过长，请缩短后重试"})
        return

    trace_id = generate_trace_id()
    logger = log.bind(trace_id=trace_id)
    logger.info("处理 user_msg ({} chars)", len(user_content))

    current_sid = session_mgr.get_current_session_id() or ""

    # WS on_token 回调：流式推送（断连时转 StreamInterrupted 保留部分回复）
    from ..agent.core import StreamInterrupted

    async def _send_token(token: str):
        try:
            await websocket.send_json({"type": "stream", "text": token})
        except WebSocketDisconnect:
            raise StreamInterrupted()

    try:
        await websocket.send_json({"type": "thinking", "session_id": current_sid})

        full_text = await agent.chat(
            user_content,
            trace_id=trace_id,
            on_token=_send_token,
            _client_snapshot=chat_client,
            _config_snapshot=chat_config,
        )

        await websocket.send_json({
            "type": "end",
            "full_text": full_text,
            "action": "idle",
            "session_id": current_sid,
        })

    except StreamInterrupted:
        # on_token 内部 catch → raise StreamInterrupted
        # Agent.chat() 已保存部分回复，无需额外处理
        logger.info("客户端断开，部分回复已由 Agent 保存")

    except WebSocketDisconnect:
        # thinking/end 报文发送时客户端断开（token 流式未受影响）
        logger.info("客户端断开（非流式阶段）")

    except BlockedError as e:
        await websocket.send_json({
            "type": "blocked",
            "message": e.message,
            "category": e.category,
            "session_id": current_sid,
        })

    except ValueError as e:
        await websocket.send_json({
            "type": "error",
            "message": str(e),
            "session_id": current_sid,
        })

    except asyncio.CancelledError:
        raise

    except Exception as e:
        logger.opt(exception=True).error("流式生成异常: {}", e)
        try:
            await websocket.send_json({
                "type": "error",
                "message": "AI 服务暂时不可用，请稍后重试",
                "session_id": current_sid,
            })
        except Exception as send_err:
            logger.warning("发送 error 报文失败（连接可能已断）: {}", send_err)


# ── 辅助函数 ─────────────────────────────────────────────────

def _ensure_session(session_mgr: SessionManager, target_id: str,
                    agent: Optional[Agent] = None,
                    persona_guard=None) -> bool:
    """确保当前会话正确。返回 True=就绪，False=目标会话不存在。
    会话切换时清理角色漂移状态（对齐 CLI /new 和 /switch 的行为）。
    """
    if not target_id:
        session_mgr.flush()
        session_mgr.create_session()
        if agent:
            agent._pending_anchor = None  # type: ignore[attr-defined]
            if agent.mood_engine:
                agent.mood_engine.reset_state()
            if agent.bond_tracker:
                agent.bond_tracker.reset_state()
        if persona_guard:
            persona_guard.reset_state()
        return True

    if session_mgr.get_current_session_id() != target_id:
        session_mgr.flush()
        loaded = session_mgr.load_session(target_id)
        if loaded is None:
            return False
        if agent:
            agent._pending_anchor = None  # type: ignore[attr-defined]
            if agent.mood_engine:
                agent.mood_engine.reset_state()
            if agent.bond_tracker:
                agent.bond_tracker.reset_state()
        if persona_guard:
            persona_guard.reset_state()
    return True


def _format_session_list(raw_list: list[dict]) -> list[dict]:
    """SessionManager.list_sessions() 返回值 → 协议格式（session_id→id, datetime→str）"""
    return [
        {
            "id": s.get("session_id", ""),
            "title": s.get("title", ""),
            "message_count": s.get("message_count", 0),
            "created_at": _fmt_dt(s.get("created_at")),
            "updated_at": _fmt_dt(s.get("updated_at")),
        }
        for s in raw_list
    ]


def _fmt_dt(val) -> str:
    """datetime 对象 → ISO 8601 字符串"""
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def _validate_config(cfg: dict, label: str) -> list[str]:
    """校验配置字段。只检查用户明确传了值的字段（null 表示不更新，跳过）"""
    errors = []
    # api_key: 如果传了值，不能为空
    ak = cfg.get("api_key")
    if ak is not None and (not isinstance(ak, str) or not ak.strip()):
        errors.append(f"{label} API Key 不能为空")
    # base_url: 如果传了值，必须以 http 开头
    url = cfg.get("base_url")
    if url is not None and (not isinstance(url, str) or not url.strip().startswith(("http://", "https://"))):
        errors.append(f"{label} Base URL 格式不正确（需以 http:// 或 https:// 开头）")
    # model: 如果传了值，不能为空
    model = cfg.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        errors.append(f"{label} 模型名不能为空")
    return errors


def _is_ws_request_allowed(websocket: WebSocket) -> bool:
    """校验本地 WS 访问来源。Electron 启动时必须携带一次性 token。"""
    expected = get_or_create_ws_token(log)
    if not expected:
        if os.environ.get("FENGJIN_WS_ALLOW_UNAUTH_DEV", "").lower() in ("1", "true", "yes"):
            log.warning("WS 无鉴权开发模式已启用，仅建议本地临时调试使用")
        else:
            log.warning("拒绝 WS 连接: token 未配置")
            return False
    else:
        supplied = websocket.query_params.get("token", "")
        if not secrets.compare_digest(supplied, expected):
            log.warning("拒绝 WS 连接: token 无效")
            return False

    origin = websocket.headers.get("origin", "")
    if not origin:
        return True
    allowed_prefixes = (
        "file://",
        "http://localhost",
        "http://127.0.0.1",
        "http://[::1]",
        "https://localhost",
        "https://127.0.0.1",
        "https://[::1]",
    )
    if origin.startswith(allowed_prefixes):
        return True
    log.warning("拒绝 WS 连接: Origin 不可信 ({})", origin)
    return False
