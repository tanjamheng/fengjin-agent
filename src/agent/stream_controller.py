"""流式生成控制器 — 取消信号 + 部分文本追踪

传输层（ws/connection.py）和 service 层（agent/streaming.py）共享，
由调用方（传输层）置位 cancel_requested，service 层在每个 token 前检查。
"""

from dataclasses import dataclass


@dataclass
class StreamController:
    """控制单个流式生成任务

    职责:
    1. 提供 cancel_requested 标志供 token 循环检查
    2. 追踪已收到的部分文本（取消后需要保存到上下文）
    """
    cancel_requested: bool = False
    partial_text: str = ""       # 已收到的部分回复（取消后写入上下文）
    tokens_received: int = 0

    def cancel(self) -> None:
        """请求取消，非阻塞"""
        self.cancel_requested = True

    def add_token(self, text: str) -> None:
        """记录收到的 token"""
        self.partial_text += text
        self.tokens_received += 1
