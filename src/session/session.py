"""会话数据定义"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class MessageMeta(BaseModel):
    """消息附带数据，类型安全"""
    emotion: Optional[str] = None
    rag_hits: Optional[list[str]] = None
    memory_used: Optional[list[str]] = None


class Message(BaseModel):
    """单条消息"""
    role: str               # "user" | "assistant"
    content: str
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: MessageMeta = Field(default_factory=MessageMeta)


class Session(BaseModel):
    """一个完整会话"""
    session_id: str
    title: str
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    messages: list[Message] = Field(default_factory=list)

    def add_message(self, role: str, content: str, metadata: Optional[MessageMeta] = None) -> Message:
        """追加一条消息并更新时间戳"""
        msg = Message(
            role=role,
            content=content,
            metadata=metadata or MessageMeta(),
        )
        self.messages.append(msg)
        self.updated_at = datetime.now()
        return msg

    @property
    def message_count(self) -> int:
        return len(self.messages)
