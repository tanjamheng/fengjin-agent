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
        small_client = MemorySettings.create_mind_model_client()
        self.client = small_client
        model_name = MemorySettings.get_mind_model_name()

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
            small_client.close()
            raise
        self.log = get_logger("memory_manager")
        self._extract_threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def retrieve(self, user_input: str, trace_id: str = "") -> str:
        """检索记忆，返回格式化的记忆文本（用于注入 system prompt）"""
        return self.retriever.retrieve(user_input, trace_id=trace_id)

    def extract_async(self, user_input: str, assistant_message: str,
                      trace_id: str = "") -> None:
        """异步提取记忆并写入（不阻塞主流程）"""
        log = self.log.bind(trace_id=trace_id) if trace_id else self.log

        def _worker():
            t_start = time.time()
            try:
                log.debug("异步记忆提取开始")
                facts = self.extractor.extract(user_input, assistant_message, trace_id=trace_id)
                t_extract = (time.time() - t_start) * 1000
                if facts:
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

    def extract_conversation_async(self, conversation_text: str,
                                   trace_id: str = "", on_model_failure=None,
                                   should_apply=None) -> None:
        """从已经整理好的多轮自然对话异步提取记忆。"""
        log = self.log.bind(trace_id=trace_id) if trace_id else self.log

        def _worker():
            t_start = time.time()
            try:
                facts = self.extractor.extract_conversation(conversation_text, trace_id=trace_id)
                elapsed = (time.time() - t_start) * 1000
                if facts and should_apply is not None and not should_apply():
                    log.info("丢弃已失效的记忆提取结果")
                elif facts and self.writer._running:
                    self.writer.write(facts, should_apply=should_apply)
                    log.info("异步记忆提取完成: {} 条事实 ({:.0f}ms)", len(facts), elapsed)
                elif not facts:
                    log.debug("异步记忆提取: 无新事实 ({:.0f}ms)", elapsed)
            except Exception as e:
                log.opt(exception=True).error("记忆提取失败: {}", e)
                if on_model_failure and _is_model_error(e):
                    on_model_failure(getattr(e, "status_code", None) in (401, 403, 404))

        with self._lock:
            self._extract_threads = [t for t in self._extract_threads if t.is_alive()]
            thread = threading.Thread(target=_worker, daemon=True)
            self._extract_threads.append(thread)
        thread.start()

    def cleanup(self) -> None:
        """停止写入线程并关闭存储（防御部分初始化：任意属性缺失时跳过对应步骤）"""
        if hasattr(self, "_lock") and hasattr(self, "_extract_threads"):
            with self._lock:
                active = [t for t in self._extract_threads if t.is_alive()]
            deadline = time.monotonic() + 10.0
            for t in active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                t.join(timeout=remaining)
            still_alive = sum(1 for t in active if t.is_alive())
            if still_alive:
                self.log.warning("{} 个记忆提取任务在清理超时后仍未结束，将丢弃其结果", still_alive)
            self._extract_threads.clear()
        if hasattr(self, "writer") and self.writer is not None:
            self.writer.stop()
            self.writer = None
        if hasattr(self, "storage") and self.storage is not None:
            self.storage.cleanup()
            self.storage = None
        if hasattr(self, "client") and self.client is not None:
            try:
                self.client.close()
            except Exception as e:
                self.log.warning("记忆模型客户端关闭异常: {}", e)
            self.client = None
        self.extractor = None
        self.retriever = None


def _is_model_error(exc: Exception) -> bool:
    return (
        exc.__class__.__module__.startswith("openai")
        or exc.__class__.__name__ == "MemoryModelOutputError"
        or hasattr(exc, "status_code")
    )
