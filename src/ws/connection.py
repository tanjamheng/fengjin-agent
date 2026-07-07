"""WebSocket 端点 — 消息路由 + 报文映射（瘦传输层）

只负责：协议消息路由、报文收发、会话 CRUD 路由、对话事件→报文映射。
对话业务逻辑（安全 / 上下文 / LLM / Tool / 落盘）在 Agent.chat()（core.py）。
"""

import json
import os
import asyncio
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..session import SessionManager
from ..config import Config
from ..agent.context_manager import ContextManager
from ..agent.core import Agent, BlockedError, MAX_INPUT_LENGTH
from ..utils.logger import get_logger, generate_trace_id

router = APIRouter()
log = get_logger("ws")

HEARTBEAT_TIMEOUT = 45       # 秒，必须大于前端 ping 间隔(30s) + pong 超时(10s)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    log.info("WebSocket 客户端已连接")

    # 应用级单例（启动时加载，含 GPU 模型，所有连接共享）
    config = websocket.app.state.config
    client = websocket.app.state.client
    safety = websocket.app.state.safety

    # 每连接独立：会话管理器 + 上下文管理器 + Agent
    from pathlib import Path
    _sessions_dir = str(Path(__file__).resolve().parent.parent.parent / "data" / "sessions")
    session_mgr = SessionManager(data_dir=_sessions_dir)
    memory_mgr = getattr(websocket.app.state, "memory_manager", None)
    context_mgr = ContextManager(
        websocket.app.state.context_config,
        memory_retriever=memory_mgr.retriever if memory_mgr else None,
    )
    tool_registry = getattr(websocket.app.state, "tool_registry", None)
    mood_engine = getattr(websocket.app.state, "mood_engine", None)
    bond_tracker = getattr(websocket.app.state, "bond_tracker", None)
    persona_guard = getattr(websocket.app.state, "persona_guard", None)

    # Agent — CLI/WS 共用的对话入口（chat() 内部处理安全/记忆/上下文/LLM/Tool/落盘）
    agent = Agent(
        config=config,
        session_mgr=session_mgr,
        safety=safety,
        client=client,
        context_manager=context_mgr,
        memory_manager=memory_mgr,
        tool_registry=tool_registry,
        mood_engine=mood_engine,
        bond_tracker=bond_tracker,
        persona_guard=persona_guard,
    )

    # 不预先创建会话——等用户发送第一条消息时才创建
    await websocket.send_json({
        "type": "connected",
        "session_id": "",
    })

    # 流任务状态
    current_stream: Optional[asyncio.Task] = None

    # 心跳追踪
    last_pong = asyncio.get_event_loop().time()

    async def _heartbeat_checker():
        """后台心跳超时检测：每 15s 检查一次，超过 HEARTBEAT_TIMEOUT 无 pong 则断开
        注意：前端 ping 间隔 30s，后端超时阈值必须 > 30s 避免竞态断连
        """
        while True:
            await asyncio.sleep(15)
            elapsed = asyncio.get_event_loop().time() - last_pong
            if elapsed > HEARTBEAT_TIMEOUT:
                log.warning("心跳超时（{:.0f}s），关闭连接", elapsed)
                try:
                    await websocket.close()
                except Exception as e:
                    log.debug("心跳关闭连接异常: {}", e)
                break

    heartbeat_task = asyncio.create_task(_heartbeat_checker())

    try:
        async for raw in websocket.iter_text():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("收到非法 JSON: {}", raw[:100])
                continue

            msg_type = data.get("type")

            # ── ping ──
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
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
                        await current_stream
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
                                "content": m.content,
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
                target_id = data.get("session_id", "")
                # 若删除的是当前会话，清理漂移保护状态（对齐 CLI /delete 行为）
                if target_id and target_id == session_mgr.get_current_session_id():
                    agent._pending_anchor = None
                    if persona_guard:
                        persona_guard.reset_state()
                    if agent.mood_engine:
                        agent.mood_engine.reset_state()
                    if agent.bond_tracker:
                        agent.bond_tracker.reset_state()
                session_mgr.delete_session(target_id)
                await websocket.send_json({
                    "type": "session_deleted",
                    "session_id": target_id,
                })

            # ── get_config ──
            elif msg_type == "get_config":
                from ..server.config_manager import ConfigManager
                cfg = ConfigManager.get_current_config()
                await websocket.send_json({
                    "type": "current_config",
                    **cfg,
                })

            # ── update_config ──
            elif msg_type == "update_config":
                from ..server.config_manager import ConfigManager

                main_cfg = data.get("main", {})
                memory_cfg = data.get("memory", {})
                memory_enabled = bool(data.get("memory_enabled", False))

                # 校验主模型
                errors = _validate_config(main_cfg, "主模型")
                if memory_enabled:
                    errors.extend(_validate_config(memory_cfg, "记忆模型"))

                if errors:
                    await websocket.send_json({
                        "type": "config_updated",
                        "success": False,
                        "errors": errors,
                    })
                    continue

                # 保存旧 os.environ 快照（用于 rebuild 失败时回滚）
                _old_environ = {k: os.environ.get(k) for k in [
                    "FENGJIN_API_KEY", "FENGJIN_BASE_URL", "FENGJIN_MODEL",
                    "MEMO_API_KEY", "MEMO_BASE_URL", "MEMO_MODEL", "MEMORY_ENABLED",
                ]}

                # 先更新 os.environ（rebuild 内部 _reload_dotenv 需读取新值）
                ConfigManager.apply_to_os_environ(main_cfg, memory_cfg, memory_enabled)

                # 重建客户端
                try:
                    await ConfigManager.rebuild_clients(
                        websocket.app, main_cfg, memory_cfg, memory_enabled,
                    )
                except Exception as e:
                    log.opt(exception=True).error("配置热更新失败: {}", e)
                    # 回滚 os.environ 到旧值
                    for k, v in _old_environ.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                    await websocket.send_json({
                        "type": "config_updated",
                        "success": False,
                        "errors": ["配置热更新失败，请重启后端"],
                    })
                    continue

                # 成功后：写 .env（持久化）+ 更新局部引用 + 更新已有 Agent
                env_persisted = ConfigManager.update_env_file(main_cfg, memory_cfg, memory_enabled)
                client = websocket.app.state.client
                memory_mgr = getattr(websocket.app.state, "memory_manager", None)
                context_mgr = ContextManager(
                    websocket.app.state.context_config,
                    memory_retriever=memory_mgr.retriever if memory_mgr else None,
                )
                # 更新当前连接已有 Agent 的引用（后续消息使用新配置）
                agent.client = client
                agent.context_manager = context_mgr
                agent.memory_manager = memory_mgr

                # 持久化失败：运行时已生效，但重启后回滚——必须告知用户（红线8：静默失败零容忍）
                if not env_persisted:
                    await websocket.send_json({
                        "type": "config_updated",
                        "success": False,
                        "errors": ["配置已生效，但持久化失败，重启后端后将回滚"],
                    })
                    continue

                await websocket.send_json({
                    "type": "config_updated",
                    "success": True,
                })

    except WebSocketDisconnect:
        log.info("客户端主动断开")
    except Exception as e:
        log.opt(exception=True).error("WebSocket 异常: {}", e)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
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
        log.info("WebSocket 连接关闭，会话已保存")


# ── 对话事件 → 报文映射 ──────────────────────────────────────

async def _handle_user_msg(
    websocket: WebSocket,
    data: dict,
    agent: Agent,
    session_mgr: SessionManager,
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
    logger.info("处理 user_msg: {}", user_content[:50])

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
