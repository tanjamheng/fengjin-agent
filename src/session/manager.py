"""会话生命周期管理 + 事务编排"""

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .session import Session, Message, MessageMeta
from .store import SessionStore
from ..utils.logger import get_logger


class SessionManager:
    """会话管理器

    封装所有会话操作的业务逻辑，决定什么时候落盘。
    append_message() 只改内存，flush() 显式落盘。
    """

    def __init__(self, data_dir: str = "data/sessions"):
        self.store = SessionStore(data_dir)
        self.current_session: Optional[Session] = None
        self.log = get_logger("session")

    # ── 生命周期 ─────────────────────────────────────────

    def create_session(self, title: Optional[str] = None) -> Session:
        """创建新会话"""
        session = Session(
            session_id=str(uuid.uuid4()),
            title=title or f"新会话 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        )
        self.current_session = session
        self.store.save_session(session)
        self.log.info(f"创建会话: {session.session_id}")
        return session

    def load_session(self, session_id: str) -> Optional[Session]:
        """加载旧会话"""
        session = self.store.load_session(session_id)
        if session:
            self.current_session = session
            self.log.info(f"加载会话: {session_id}, 消息数: {session.message_count}")
        return session

    def list_sessions(self) -> list[dict]:
        """扫描目录，从每个 session 文件读取元数据，按 updated_at 倒序"""
        sessions = []
        for path in self.store.list_session_files():
            session = self.store.load_session(path.stem)
            if session:
                sessions.append({
                    "session_id": session.session_id,
                    "title": session.title,
                    "message_count": session.message_count,
                    "updated_at": session.updated_at,
                })
        sessions.sort(key=lambda s: s["updated_at"], reverse=True)
        return sessions

    def rename_session(self, session_id: str, title: str) -> bool:
        """重命名会话"""
        session = self.store.load_session(session_id)
        if not session:
            return False
        session.title = title
        self.store.save_session(session)
        if self.current_session and self.current_session.session_id == session_id:
            self.current_session = session
        self.log.info(f"重命名会话: {session_id} -> {title}")
        return True

    def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        if self.current_session and self.current_session.session_id == session_id:
            self.current_session = None
        return self.store.delete_session(session_id)

    # ── 消息操作（只改内存）─────────────────────────────────

    def append_message(self, role: str, content: str, metadata: Optional[MessageMeta] = None) -> None:
        """追加消息到当前会话（只改内存，不落盘）

        自动用第一条用户消息前 20 字作为默认标题。
        """
        if not self.current_session:
            self.create_session()

        # 自动标题：第一条用户消息前 20 字
        if (self.current_session.title.startswith("新会话")
                and role == "user"
                and not any(m.role == "user" for m in self.current_session.messages)):
            auto_title = content[:20].replace("\n", " ").strip()
            if auto_title:
                self.current_session.title = auto_title

        self.current_session.add_message(role, content, metadata)

    def flush(self) -> None:
        """显式落盘：把当前会话写入文件"""
        if self.current_session:
            self.store.save_session(self.current_session)

    # ── 查询 ─────────────────────────────────────────────

    def get_current_session_id(self) -> Optional[str]:
        if self.current_session:
            return self.current_session.session_id
        return None

    def get_current_messages(self) -> list[dict]:
        """获取当前会话的全部消息（dict 格式，兼容 Agent.messages）"""
        if not self.current_session:
            return []
        return [
            {"role": msg.role, "content": msg.content}
            for msg in self.current_session.messages
        ]

    def get_recent_messages(self, n: int = 10) -> list[Message]:
        """获取当前会话最近 N 条消息"""
        if not self.current_session:
            return []
        return self.current_session.messages[-n:]
