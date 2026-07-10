"""记忆管理器（门面类）"""

import threading
import time

from .config import MemoryConfig, MemorySettings
from .storage import MemoryStorage
from .extractor import MemoryExtractor
from .writer import MemoryWriter
from .retriever import MemoryRetriever
from ..utils.logger import get_logger


class MemoryManager:
    """记忆系统门面

    对外暴露两个方法：
    - retrieve(user_input) -> str：检索记忆，返回注入文本
    - extract_async(user_input, assistant_message)：异步提取+写入
    """

    def __init__(self, config: MemoryConfig):
        small_client = MemorySettings.create_memo_model_client()
        model_name = MemorySettings.get_memo_model_name()

        self.storage = None
        self.extractor = None
        self.writer = None
        self.retriever = None
        try:
            self.storage = MemoryStorage(config)
            self.extractor = MemoryExtractor(config, small_client, model_name, self.storage)
            self.writer = MemoryWriter(config, small_client, model_name, self.storage)
            self.retriever = MemoryRetriever(config, self.storage)
        except Exception:
            # 部分初始化失败：清理已初始化的组件
            if self.writer:
                self.writer.stop()
            if self.storage:
                self.storage.cleanup()
            raise
        self.log = get_logger("memory_manager")
        self._extract_threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def retrieve(self, user_input: str, trace_id: str = "") -> str:
        """检索记忆，返回格式化的记忆文本（用于注入 system prompt）"""
        return self.retriever.retrieve(user_input, trace_id=trace_id)

    def extract_async(self, user_input: str, assistant_message: str,
                      trace_id: str = "", session_id: str = "") -> None:
        """异步提取记忆并写入（不阻塞主流程）"""
        log = self.log.bind(trace_id=trace_id) if trace_id else self.log

        def _worker():
            t_start = time.time()
            try:
                log.debug("异步记忆提取开始")
                facts = self.extractor.extract(user_input, assistant_message, trace_id=trace_id)
                t_extract = (time.time() - t_start) * 1000
                if facts:
                    if session_id:
                        for fact in facts:
                            fact["session_id"] = session_id
                    if self.writer._running:
                        self.writer.write(facts)
                        log.info("异步记忆提取完成: {} 条事实 ({:.0f}ms)", len(facts), t_extract)
                    else:
                        log.warning("异步记忆提取跳过: writer 已停止")
                else:
                    log.debug("异步记忆提取: 无新事实 ({:.0f}ms)", t_extract)
            except Exception as e:
                self.log.opt(exception=True).error("记忆提取失败 [trace={}]: {}", trace_id, e)

        # 清理已完成线程，防止列表无界增长
        with self._lock:
            self._extract_threads = [t for t in self._extract_threads if t.is_alive()]

        thread = threading.Thread(target=_worker, daemon=True)
        with self._lock:
            self._extract_threads.append(thread)
        thread.start()

    def delete_session_memories(self, session_id: str) -> None:
        """删除指定会话派生出的长期记忆。"""
        if not session_id or not self.writer:
            return
        self.writer.delete_session_memories(session_id)

    def cleanup(self) -> None:
        """停止写入线程并关闭存储（防御部分初始化：任意属性缺失时跳过对应步骤）"""
        if hasattr(self, "_lock") and hasattr(self, "_extract_threads"):
            with self._lock:
                active = [t for t in self._extract_threads if t.is_alive()]
            for t in active:
                t.join(timeout=10)
            self._extract_threads.clear()
        if hasattr(self, "writer") and self.writer is not None:
            self.writer.stop()
        if hasattr(self, "storage") and self.storage is not None:
            self.storage.cleanup()
