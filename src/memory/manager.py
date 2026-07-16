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
from ..mind.model_runtime import MindModelRuntime
from .retriever import MemoryRetriever
from ..utils.logger import get_logger


@dataclass(frozen=True)
class _ConversationTask:
    task_id: str
    conversation_text: str
    trace_id: str
    on_model_failure: Optional[Callable[[bool, bool], None]]
    should_apply: Optional[Callable[[], bool]]


class MemoryManager:
    """记忆系统门面

    对外暴露两个方法：
    - retrieve(user_input) -> str：检索记忆，返回注入文本
    - extract_async(user_input, assistant_message)：异步提取+写入
    """

    def __init__(self, config: MemoryConfig, *, client=None,
                 model_name: str | None = None, max_retries: int = 3,
                 queue_warning_threshold: int = 10,
                 runtime: MindModelRuntime | None = None):
        self._cleanup_complete = threading.Event()
        model_name = model_name or MemorySettings.get_mind_model_name()
        if runtime is None:
            small_client = client or MemorySettings.create_mind_model_client()
            runtime = MindModelRuntime.single_client(small_client, model_name)
            self._owns_runtime = True
        else:
            self._owns_runtime = False
        self.model_runtime = runtime

        self.storage = None
        self.extractor = None
        self.writer = None
        self.retriever = None
        try:
            self.storage = MemoryStorage(config)
            self.extractor = MemoryExtractor(
                config, None, model_name, self.storage,
                max_retries=max_retries,
                runtime=runtime,
            )
            self.writer = MemoryWriter(
                config, None, model_name, self.storage,
                max_retries=max_retries,
                runtime=runtime,
            )
            self.retriever = MemoryRetriever(config, self.storage)
        except Exception:
            # 部分初始化失败：清理已初始化的组件
            if self.writer:
                self.writer.stop()
            if self.storage:
                self.storage.cleanup()
            if self._owns_runtime:
                runtime.close()
            raise
        self.log = get_logger("memory_manager")
        self._extract_threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._conversation_queue: queue.Queue[_ConversationTask | None] = queue.Queue()
        self._queue_warning_threshold = queue_warning_threshold
        self._next_queue_warning = queue_warning_threshold
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
        queue_size = self._conversation_queue.qsize()
        self.log.debug(
            "记忆提取任务已提交: {} (queue={})",
            task.task_id, queue_size,
        )
        warning_threshold = getattr(self, "_queue_warning_threshold", 10)
        next_warning = getattr(self, "_next_queue_warning", warning_threshold)
        if queue_size >= next_warning:
            self.log.warning(
                "记忆提取任务积压: queue={} threshold={}（保持 FIFO，不丢任务）",
                queue_size, next_warning,
            )
            self._next_queue_warning = max(next_warning * 2, queue_size + 1)

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
                runtime_version = None
                with self.model_runtime.acquire("memory") as lease:
                    runtime_version = lease.version
                    facts = self.extractor.extract_conversation(
                        task.conversation_text,
                        trace_id=task.trace_id,
                        runtime_lease=lease,
                    )
                elapsed = (time.time() - t_start) * 1000
                if facts and not _can_apply(task.should_apply):
                    log.info("丢弃已失效的记忆提取结果: {}", task.task_id)
                elif facts and self.writer._running:
                    self.writer.write(
                        facts,
                        should_apply=task.should_apply,
                    )
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
                    current_runtime = (
                        runtime_version is not None
                        and self.model_runtime.is_current(runtime_version)
                    )
                    task.on_model_failure(
                        getattr(e, "status_code", None) in (401, 403, 404),
                        current_runtime,
                    )
            finally:
                self._conversation_queue.task_done()
                threshold = getattr(self, "_queue_warning_threshold", 10)
                if self._conversation_queue.qsize() < threshold:
                    self._next_queue_warning = threshold

    def cleanup(self) -> None:
        """停止写入线程并关闭存储（防御部分初始化：任意属性缺失时跳过对应步骤）"""
        if getattr(self, "_cleanup_started", False):
            return
        self._cleanup_started = True
        if hasattr(self, "_conversation_stop"):
            self._conversation_stop.set()
        worker = getattr(self, "_conversation_worker", None)
        if worker is not None and worker.is_alive():
            self._conversation_queue.put(None)
            worker.join(timeout=10.0)
            if worker.is_alive():
                self.log.warning("记忆 FIFO Worker 清理超时，资源将在请求结束后延迟释放")
                threading.Thread(
                    target=self._deferred_cleanup,
                    args=(worker,),
                    name="mind-memory-deferred-cleanup",
                    daemon=True,
                ).start()
                return
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
                self.log.warning("{} 个记忆提取任务仍在运行，资源将在任务结束后延迟释放", still_alive)
                threading.Thread(
                    target=self._deferred_legacy_cleanup,
                    args=(active,),
                    name="mind-memory-legacy-cleanup",
                    daemon=True,
                ).start()
                return
            self._extract_threads.clear()
        self._finish_cleanup()

    def _deferred_cleanup(self, worker: threading.Thread) -> None:
        worker.join()
        self._conversation_worker = None
        self._finish_cleanup()

    def _deferred_legacy_cleanup(self, threads: list[threading.Thread]) -> None:
        for thread in threads:
            thread.join()
        self._extract_threads.clear()
        self._finish_cleanup()

    def _finish_cleanup(self) -> None:
        if hasattr(self, "writer") and self.writer is not None:
            writer = self.writer
            self.writer = None
            try:
                writer.stop()
            except Exception as exc:
                self.log.opt(exception=True).error("记忆 Writer 停止异常: {}", exc)
            writer_thread = getattr(writer, "_thread", None)
            if writer_thread is not None and writer_thread.is_alive():
                self.log.warning("记忆 Writer 仍在运行，存储将在写入结束后延迟释放")
                threading.Thread(
                    target=self._deferred_writer_cleanup,
                    args=(writer_thread,),
                    name="mind-memory-writer-cleanup",
                    daemon=True,
                ).start()
                return
        self._finish_storage_cleanup()

    def _deferred_writer_cleanup(self, writer_thread: threading.Thread) -> None:
        writer_thread.join()
        self._finish_storage_cleanup()

    def _finish_storage_cleanup(self) -> None:
        if hasattr(self, "storage") and self.storage is not None:
            try:
                self.storage.cleanup()
            except Exception as exc:
                self.log.opt(exception=True).error("记忆存储清理异常: {}", exc)
            self.storage = None
        if getattr(self, "_owns_runtime", False):
            self.model_runtime.close()
        self.extractor = None
        self.retriever = None
        self._cleanup_complete.set()

    def wait_cleanup(self) -> None:
        """等待所有旧代 Writer/Storage 完全释放；热更新创建新代前调用。"""
        self._cleanup_complete.wait()


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
