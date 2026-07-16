"""记忆管理器（门面类）"""

import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from .config import MemoryConfig, MemorySettings
from .storage import MemoryStorage
from .extractor import MemoryExtractor
from .writer import MemoryWriter
from .retriever import MemoryRetriever
from ..utils.logger import get_logger


@dataclass(frozen=True)
class _ConversationTask:
    task_id: str
    conversation_text: str
    trace_id: str
    on_model_failure: Optional[Callable[[bool], None]]
    should_apply: Optional[Callable[[], bool]]


class MemoryManager:
    """记忆系统门面

    对外暴露两个方法：
    - retrieve(user_input) -> str：检索记忆，返回注入文本
    - extract_async(user_input, assistant_message)：异步提取+写入
    """

    def __init__(self, config: MemoryConfig, *, client=None, model_name: str | None = None):
        small_client = client or MemorySettings.create_mind_model_client()
        self.client = small_client
        model_name = model_name or MemorySettings.get_mind_model_name()

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
        self._conversation_queue: queue.Queue[_ConversationTask | None] = queue.Queue()
        self._conversation_stop = threading.Event()
        self._conversation_worker = threading.Thread(
            target=self._conversation_loop,
            name="mind-memory-worker",
            daemon=True,
        )
        try:
            self._conversation_worker.start()
        except Exception:
            # Worker 创建也属于初始化链：失败时不得遗留 writer、Chroma 或客户端资源。
            self.cleanup()
            raise

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
        """提交到独立 FIFO Worker；与状态分析并行，但记忆轮次不乱序。"""
        if self._conversation_stop.is_set():
            self.log.warning("记忆提取任务提交跳过: Worker 已停止")
            return
        task = _ConversationTask(
            task_id=uuid.uuid4().hex[:8],
            conversation_text=conversation_text,
            trace_id=trace_id,
            on_model_failure=on_model_failure,
            should_apply=should_apply,
        )
        self._conversation_queue.put(task)
        self.log.debug(
            "记忆提取任务已提交: {} (queue={})",
            task.task_id, self._conversation_queue.qsize(),
        )

    def _conversation_loop(self) -> None:
        while True:
            task = self._conversation_queue.get()
            if task is None:
                self._conversation_queue.task_done()
                return
            try:
                if self._conversation_stop.is_set() or not _can_apply(task.should_apply):
                    continue
                log = self.log.bind(trace_id=task.trace_id) if task.trace_id else self.log
                t_start = time.time()
                facts = self.extractor.extract_conversation(
                    task.conversation_text, trace_id=task.trace_id
                )
                elapsed = (time.time() - t_start) * 1000
                if facts and not _can_apply(task.should_apply):
                    log.info("丢弃已失效的记忆提取结果: {}", task.task_id)
                elif facts and self.writer._running:
                    self.writer.write(facts, should_apply=task.should_apply)
                    log.info(
                        "异步记忆提取完成: task={} {} 条事实 ({:.0f}ms)",
                        task.task_id, len(facts), elapsed,
                    )
                elif not facts:
                    log.debug("异步记忆提取: 无新事实 ({:.0f}ms)", elapsed)
            except Exception as e:
                self.log.opt(exception=True).error(
                    "记忆提取失败 [task={} trace={}]: {}", task.task_id, task.trace_id, e
                )
                if task.on_model_failure and _is_model_error(e):
                    task.on_model_failure(getattr(e, "status_code", None) in (401, 403, 404))
            finally:
                self._conversation_queue.task_done()

    def cleanup(self) -> None:
        """停止写入线程并关闭存储（防御部分初始化：任意属性缺失时跳过对应步骤）"""
        if hasattr(self, "_conversation_stop"):
            self._conversation_stop.set()
        worker = getattr(self, "_conversation_worker", None)
        if worker is not None and worker.is_alive():
            self._conversation_queue.put(None)
            worker.join(timeout=10.0)
            if worker.is_alive():
                self.log.warning("记忆 FIFO Worker 清理超时，将丢弃未完成结果")
        self._conversation_worker = None
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


def _can_apply(should_apply) -> bool:
    if should_apply is None:
        return True
    try:
        return bool(should_apply())
    except Exception:
        return False
