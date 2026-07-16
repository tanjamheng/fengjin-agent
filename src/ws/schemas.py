"""WebSocket 协议消息的 Pydantic 模型"""

from typing import Optional, Union

from pydantic import BaseModel


# ── 前端 → 后端 ──────────────────────────────────────────────

class UserMessage(BaseModel):
    type: str = "user_msg"
    session_id: str = ""
    content: str


class PingMessage(BaseModel):
    type: str = "ping"


class CancelMessage(BaseModel):
    type: str = "cancel"


class ListSessionsMessage(BaseModel):
    type: str = "list_sessions"


class LoadSessionMessage(BaseModel):
    type: str = "load_session"
    session_id: str


class DeleteSessionMessage(BaseModel):
    type: str = "delete_session"
    session_id: str


class RenameSessionMessage(BaseModel):
    type: str = "rename_session"
    session_id: str
    title: str


# ── 后端 → 前端 ──────────────────────────────────────────────

class ConnectedMessage(BaseModel):
    type: str = "connected"
    session_id: str


class PongMessage(BaseModel):
    type: str = "pong"


class ThinkingMessage(BaseModel):
    type: str = "thinking"


class BlockedMessage(BaseModel):
    type: str = "blocked"
    message: str
    category: Optional[str] = None


class StreamChunk(BaseModel):
    type: str = "stream"
    text: str


class StreamEnd(BaseModel):
    type: str = "end"
    full_text: str
    action: str = "idle"


class SessionMeta(BaseModel):
    id: str
    title: str
    message_count: int
    created_at: str
    updated_at: str


class SessionListMessage(BaseModel):
    type: str = "session_list"
    sessions: list[SessionMeta]


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str
    timestamp: str


class SessionLoadedMessage(BaseModel):
    type: str = "session_loaded"
    session_id: str
    title: str
    messages: list[ChatMessage]


class SessionDeletedMessage(BaseModel):
    type: str = "session_deleted"
    session_id: str


class SessionRenamedMessage(BaseModel):
    type: str = "session_renamed"
    session_id: str
    title: str


class QuickRepliesMessage(BaseModel):
    type: str = "quick_replies"
    replies: list[str]


class ErrorMessage(BaseModel):
    type: str = "error"
    message: str


class MindWarningMessage(BaseModel):
    type: str = "mind_warning"
    message: str


# ── 联合类型 ─────────────────────────────────────────────────

ServerMessage = Union[
    ConnectedMessage,
    PongMessage,
    ThinkingMessage,
    BlockedMessage,
    StreamChunk,
    StreamEnd,
    SessionListMessage,
    SessionLoadedMessage,
    SessionDeletedMessage,
    SessionRenamedMessage,
    QuickRepliesMessage,
    ErrorMessage,
    MindWarningMessage,
]

ClientMessage = Union[
    UserMessage,
    PingMessage,
    CancelMessage,
    ListSessionsMessage,
    LoadSessionMessage,
    DeleteSessionMessage,
    RenameSessionMessage,
]
