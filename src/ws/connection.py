"""WebSocket 端点 — 消息路由 + 报文映射（瘦传输层）

只负责：协议消息路由、报文收发、会话 CRUD 路由、对话事件→报文映射。
对话业务逻辑（安全检测 / 上下文组装 / LLM 流式 / 取消）在 agent/streaming.py。
"""

import json
import asyncio
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from openai import AsyncOpenAI

from ..session import SessionManager
from ..safety import SafetyManager
from ..config import Config
from ..agent.context_manager import ContextManager
from ..agent.streaming import stream_reply, BlockedError
from ..agent.stream_controller import StreamController
from ..utils.logger import get_logger, generate_trace_id

router = APIRouter()
log = get_logger("ws")

HEARTBEAT_TIMEOUT = 10       # 秒，前端 10s 未收到 pong 判定断线
MAX_INPUT_LENGTH = 10000     # 超长输入拒绝（对齐 CLI / CLAUDE.md 技术约束）


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    log.info("WebSocket 客户端已连接")

    # 应用级单例（启动时加载，含 GPU 模型，所有连接共享）
    config = websocket.app.state.config
    client = websocket.app.state.client
    safety = websocket.app.state.safety

    # 每连接独立：会话管理器（per-user 状态）+ 上下文管理器
    session_mgr = SessionManager()
    context_mgr = ContextManager(websocket.app.state.context_config)

    # 创建新会话
    session = session_mgr.create_session()
    await websocket.send_json({
        "type": "connected",
        "session_id": session.session_id,
    })

    # 流任务状态
    current_stream: Optional[asyncio.Task] = None
    current_controller: Optional[StreamController] = None

    # 心跳追踪
    last_pong = asyncio.get_event_loop().time()

    async def _heartbeat_checker():
        """后台心跳超时检测，超时则关闭连接"""
        while True:
            await asyncio.sleep(HEARTBEAT_TIMEOUT + 5)
            elapsed = asyncio.get_event_loop().time() - last_pong
            if elapsed > HEARTBEAT_TIMEOUT + 5:
                log.warning("心跳超时，关闭连接")
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
                    current_controller.cancel()
                    try:
                        await current_stream
                    except asyncio.CancelledError:
                        pass
                    except BlockedError:
                        pass
                    except Exception as e:
                        log.opt(exception=True).error("旧流异常收尾: {}", e)

                # 切换/创建会话（目标会话不存在则报错，不继续）
                if not _ensure_session(session_mgr, data.get("session_id", "")):
                    await websocket.send_json({"type": "error", "message": "会话不存在"})
                    continue

                current_controller = StreamController()
                current_stream = asyncio.create_task(
                    _handle_user_msg(
                        websocket, data, session_mgr, safety,
                        current_controller, client, config, context_mgr,
                    )
                )

            # ── cancel ──
            elif msg_type == "cancel":
                if current_controller:
                    current_controller.cancel()
                if current_stream and not current_stream.done():
                    try:
                        await current_stream
                    except asyncio.CancelledError:
                        pass
                    except BlockedError:
                        pass
                    except Exception as e:
                        log.opt(exception=True).error("取消旧流异常: {}", e)
                partial = current_controller.partial_text if current_controller else ""
                await websocket.send_json({
                    "type": "end",
                    "full_text": partial,
                    "action": "idle",
                })

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
                session_mgr.delete_session(target_id)
                await websocket.send_json({
                    "type": "session_deleted",
                    "session_id": target_id,
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
            if current_controller:
                current_controller.cancel()
            current_stream.cancel()
            try:
                await current_stream
            except asyncio.CancelledError:
                pass

        session_mgr.flush()
        log.info("WebSocket 连接关闭，会话已保存")


# ── 对话事件 → 报文映射 ──────────────────────────────────────

async def _handle_user_msg(
    websocket: WebSocket,
    data: dict,
    session_mgr: SessionManager,
    safety: SafetyManager,
    controller: StreamController,
    client: AsyncOpenAI,
    config: Config,
    context_mgr: ContextManager,
):
    """消费 stream_reply() 的 token，映射为 WS 报文"""
    user_content = data.get("content", "")

    # 输入校验（对齐 CLI / CLAUDE.md 技术约束）
    if not isinstance(user_content, str) or not user_content.strip():
        await websocket.send_json({"type": "error", "message": "消息不能为空"})
        return
    if len(user_content) > MAX_INPUT_LENGTH:
        await websocket.send_json({"type": "error", "message": "消息过长，请缩短后重试"})
        return

    trace_id = generate_trace_id()
    logger = log.bind(trace_id=trace_id)
    logger.info("处理 user_msg: {}", user_content[:50])

    try:
        await websocket.send_json({"type": "thinking"})
        full_text = ""
        async for token in stream_reply(
            user_content, session_mgr, safety, controller, client, config, context_mgr,
            trace_id=trace_id,
        ):
            full_text += token
            await websocket.send_json({"type": "stream", "text": token})

        # 生成器正常结束（含协作式取消），service 层已落盘
        await websocket.send_json({
            "type": "end",
            "full_text": full_text,
            "action": "idle",
        })

    except BlockedError as e:
        await websocket.send_json({
            "type": "blocked",
            "message": e.message,
            "category": e.category,
        })

    except asyncio.CancelledError:
        # task.cancel() 强制中断：service 层未落盘，这里补存部分回复
        if controller.partial_text:
            session_mgr.append_message("assistant", controller.partial_text)
            session_mgr.flush()
        raise

    except Exception as e:
        logger.opt(exception=True).error("流式生成异常: {}", e)
        try:
            await websocket.send_json({
                "type": "error",
                "message": "AI 服务暂时不可用，请稍后重试",
            })
        except Exception as send_err:
            logger.warning("发送 error 报文失败（连接可能已断）: {}", send_err)


# ── 辅助函数 ─────────────────────────────────────────────────

def _ensure_session(session_mgr: SessionManager, target_id: str) -> bool:
    """确保当前会话正确。返回 True=就绪，False=目标会话不存在"""
    if not target_id:
        session_mgr.flush()
        session_mgr.create_session()
        return True

    if session_mgr.get_current_session_id() != target_id:
        session_mgr.flush()
        loaded = session_mgr.load_session(target_id)
        if loaded is None:
            return False
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
