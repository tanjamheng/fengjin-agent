"""会话管理模块

职责：
- 会话数据定义（Session, Message, MessageMeta）
- 会话持久化（JSON 文件原子读写）
- 会话生命周期管理（创建/加载/列表/切换/删除）
- 上下文恢复（进入旧会话时组装 LLM 上下文）
"""

from .session import Session, Message, MessageMeta
from .store import SessionStore
from .manager import SessionManager
from .context_restorer import ContextRestorer

__all__ = [
    "Session",
    "Message",
    "MessageMeta",
    "SessionStore",
    "SessionManager",
    "ContextRestorer",
]
