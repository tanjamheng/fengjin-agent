"""应用级心智协调器。"""

import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from .config import MindConfig, MindSettings
from .context_builder import format_turns, normalize_turns
from .model_runtime import MindModelRuntime
from .state_analyzer import MindModelError, StateAnalyzer
from ..memory.config import MemoryConfig
from ..memory.manager import MemoryManager
from ..utils.logger import get_logger

WarningCallback = Callable[[], None]


@dataclass(frozen=True)
class _StateTask:
    task_id: str
    trace_id: str
    generation: int
    turns: list[dict]
    warn: WarningCallback


class _GenerationGate:
    """把最终记忆提交与 generation 切换放在同一把锁下。"""

    def __init__(self, manager: "MindManager", generation: int):
        self.manager = manager
        self.generation = generation

    def __call__(self) -> bool:
        return self.manager._task_valid(self.generation)

    def commit(self, action) -> bool:
        # 独立 commit 锁只与 generation 切换互斥，不阻塞主对话读取/注入状态。
        with self.manager._generation_lock:
            with self.manager._lock:
                if not self.manager.active or self.generation != self.manager._generation:
                    return False
            action()
            return True


class MindManager:
    """稳定的应用级引用；热更新只替换内部服务，不替换本对象。"""

    def __init__(self, config: MindConfig, memory_config: MemoryConfig,
                 mood_engine, bond_tracker, *, max_context_tokens: int,
                 enabled: bool):
        self.config = config
        self.memory_config = memory_config
        self.mood_engine = mood_engine
        self.bond_tracker = bond_tracker
        self.max_context_tokens = max_context_tokens
        self.log = get_logger("mind")
        self._context_token_budget = max_context_tokens - config.context_reserved_tokens
        if self._context_token_budget < 256:
            self.log.error(
                "心智上下文预算不足: total={} reserved={}，心智系统将降级关闭",
                max_context_tokens, config.context_reserved_tokens,
            )
            enabled = False
        self._lock = threading.RLock()
        self._generation_lock = threading.RLock()
        self._generation = 0
        self._enabled = False
        self._ready = False
        self._cleaned = False
        self.memory_manager: MemoryManager | None = None
        self.state_analyzer: StateAnalyzer | None = None
        self.model_runtime: MindModelRuntime | None = None
        self._queue: queue.Queue[_StateTask | None] = queue.Queue()
        self._next_queue_warning = config.queue_warning_threshold
        self._next_user_warning_at = 0.0
        self._worker: threading.Thread | None = None
        self._startup_thread: threading.Thread | None = None
        self._startup_threads: set[threading.Thread] = set()
        self._retired_memory_cleanups: set[threading.Event] = set()
        self.reconfigure(enabled)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._enabled and self._ready and not self._cleaned

    @property
    def memory_retriever(self):
        return self

    def retrieve(self, user_input: str, trace_id: str = "") -> str:
        manager = self.memory_manager if self.active else None
        return manager.retrieve(user_input, trace_id=trace_id) if manager else ""

    def inject_state(self, user_input: str) -> str:
        if not self.active:
            return user_input
        try:
            with self._lock:
                value = self.bond_tracker.inject(user_input)
                return self.mood_engine.inject(value)
        except Exception as exc:
            self.log.opt(exception=True).error("心智状态注入失败，已跳过本轮注入: {}", exc)
            return user_input

    def submit(self, messages: list[dict], trace_id: str,
               on_model_failure: Optional[WarningCallback] = None,
               *, include_state: bool = True) -> None:
        if not self.active:
            return
        try:
            turns = normalize_turns(
                messages,
                max_turns=self.config.context_turns,
                max_tokens=getattr(
                    self,
                    "_context_token_budget",
                    max(256, self.max_context_tokens - self.config.context_reserved_tokens),
                ),
            )
        except Exception as exc:
            self.log.opt(exception=True).error("心智上下文整理失败，已放弃本轮后台任务: {}", exc)
            return
        if not turns:
            return

        callback = _once(on_model_failure or (lambda: None))
        generation = self._generation
        generation_gate = _GenerationGate(self, generation)
        memory = self.memory_manager
        if memory:
            try:
                memory.extract_conversation_async(
                    format_turns(turns), trace_id=trace_id,
                    on_model_failure=lambda permanent=False, current_runtime=True: self._handle_model_failure(
                        generation, callback, permanent, current_runtime
                    ),
                    should_apply=generation_gate,
                )
            except Exception as exc:
                self.log.opt(exception=True).error("记忆后台任务提交失败，已跳过本轮: {}", exc)
        if include_state and self.state_analyzer:
            try:
                task = _StateTask(uuid.uuid4().hex[:8], trace_id, generation, turns, callback)
                self._queue.put(task)
                queue_size = self._queue.qsize()
                self.log.debug("心智状态任务已提交: {} (queue={})", task.task_id, queue_size)
                warning_threshold = getattr(self.config, "queue_warning_threshold", 10)
                next_warning = getattr(self, "_next_queue_warning", warning_threshold)
                if queue_size >= next_warning:
                    self.log.warning(
                        "心智状态任务积压: queue={} threshold={}（保持 FIFO，不丢任务）",
                        queue_size, next_warning,
                    )
                    self._next_queue_warning = max(next_warning * 2, queue_size + 1)
            except Exception as exc:
                self.log.opt(exception=True).error("状态后台任务提交失败，已跳过本轮: {}", exc)

    def reset_session_state(self) -> None:
        self.mood_engine.reset_state()
        self.bond_tracker.reset_state()

    def _prepare_reconfigure(self, enabled: bool) -> int:
        with self._generation_lock:
            with self._lock:
                self._cleaned = False
                self._generation += 1
                generation = self._generation
                self._enabled = False
                self._ready = False
                self.mood_engine.set_enabled(False)
                self.bond_tracker.set_enabled(False)
        self._stop_services(wait_for_memory=False)
        with self._lock:
            if self._generation == generation and not self._cleaned:
                self._enabled = enabled
        return generation

    def reconfigure(self, enabled: bool) -> None:
        """同步启停，供启动初始化和 CLI 使用。"""
        generation = self._prepare_reconfigure(enabled)
        # 关闭要立即冻结并返回；只有即将启动新代服务时才等待旧资源彻底释放。
        if not enabled:
            self.log.info("心智系统已关闭，记忆/情绪/羁绊已冻结")
            return
        self._start_services(generation)

    def reconfigure_background(
        self, enabled: bool, on_failure: Optional[WarningCallback] = None
    ) -> None:
        """设置页使用：逻辑状态立即生效，耗时初始化在后台完成。"""
        generation = self._prepare_reconfigure(enabled)
        if not enabled:
            self.log.info("心智系统已关闭，记忆/情绪/羁绊已冻结")
            return
        worker = threading.Thread(
            target=self._background_start_services,
            args=(generation, on_failure),
            name="mind-service-startup",
            daemon=True,
        )
        start_error = None
        with self._lock:
            self._startup_thread = worker
            startup_threads = getattr(self, "_startup_threads", None)
            if startup_threads is None:
                startup_threads = self._startup_threads = set()
            startup_threads.add(worker)
            # 线程登记与启动必须在同一临界区；否则 cleanup 可能 join 尚未启动的线程。
            try:
                worker.start()
            except Exception as exc:
                startup_threads.discard(worker)
                start_error = exc
        if start_error is not None:
            self.log.error("心智后台启动线程创建失败: {}", start_error)
            self._notify_user_warning(on_failure)
            return
        self.log.info("心智系统正在后台启动")

    def _background_start_services(
        self, generation: int, on_failure: Optional[WarningCallback] = None
    ) -> None:
        try:
            self._start_services(generation, on_failure)
        finally:
            with self._lock:
                getattr(self, "_startup_threads", set()).discard(
                    threading.current_thread()
                )

    def _start_services(
        self, generation: int, on_failure: Optional[WarningCallback] = None
    ) -> None:
        # 用户可能在旧代异步清理尚未结束时立即重新开启。新代 MemoryManager
        # 会复用同一 Chroma 目录，因此必须先等旧 Writer/Storage 完整退出。
        if not self._wait_retired_memory_cleanups(generation):
            return
        if not self._startup_current(generation):
            return
        memory = None
        analyzer = None
        runtime = None
        try:
            MindSettings.validate_environment()
            model_name = MindSettings.model_name()
            memory_client = MindSettings.create_client(self.config)
            try:
                state_client = MindSettings.create_client(self.config)
            except Exception:
                memory_client.close()
                raise
            runtime = MindModelRuntime(memory_client, state_client, model_name)
            memory = MemoryManager(
                self.memory_config,
                model_name=model_name,
                max_retries=self.config.max_retries,
                queue_warning_threshold=self.config.queue_warning_threshold,
                runtime=runtime,
            )
            analyzer = StateAnalyzer(
                self.config, model=model_name, runtime=runtime,
            )
            with self._lock:
                if not self._startup_current(generation):
                    stale = True
                else:
                    stale = False
                if stale:
                    pass
                else:
                    self.memory_manager = memory
                    self.state_analyzer = analyzer
                    self.model_runtime = runtime
                    self._ready = True
                    self.mood_engine.set_enabled(True)
                    self.bond_tracker.set_enabled(True)
                    self._queue = queue.Queue()
                    self._next_queue_warning = self.config.queue_warning_threshold
                    worker_queue = self._queue
                    self._worker = threading.Thread(
                        target=self._worker_loop, args=(worker_queue,), daemon=True
                    )
                    self._worker.start()
            if stale:
                self._cleanup_unpublished_services(memory, analyzer, runtime)
                return
            self.log.info("心智系统已启用")
        except Exception as exc:
            self.log.warning("心智系统配置或初始化失败，已降级关闭: {}", exc)
            self._cleanup_unpublished_services(memory, analyzer, runtime)
            should_warn = False
            with self._lock:
                # 构造后半段（含 Worker 启动）失败时也必须撤销已发布引用和状态开关。
                if self.memory_manager is memory:
                    self.memory_manager = None
                if self.state_analyzer is analyzer:
                    self.state_analyzer = None
                if self.model_runtime is runtime:
                    self.model_runtime = None
                if self._generation == generation:
                    self._worker = None
                    self._ready = False
                    self.mood_engine.set_enabled(False)
                    self.bond_tracker.set_enabled(False)
                    should_warn = True
            if should_warn:
                self._notify_user_warning(on_failure)

    def _startup_current(self, generation: int) -> bool:
        with self._lock:
            return (
                self._generation == generation
                and self._enabled
                and not self._cleaned
            )

    def _cleanup_unpublished_services(self, memory, analyzer, runtime) -> None:
        if memory:
            self._cleanup_memory(memory, wait=False)
        if analyzer:
            self._cleanup_analyzer(analyzer)
        if runtime:
            runtime.close()

    def update_model_runtime(
        self, on_failure: Optional[WarningCallback] = None
    ) -> None:
        """热切换 Key/Base URL/模型；保留队列和在途任务。"""
        with self._lock:
            if not self._enabled or self._cleaned:
                return
            runtime = self.model_runtime
        if runtime is None:
            # 后台启用尚未发布运行时时，以新 generation 取代旧启动任务，
            # 确保最终只会发布最新环境变量对应的客户端。
            self.reconfigure_background(True, on_failure)
            return

        MindSettings.validate_environment()
        model_name = MindSettings.model_name()
        memory_client = MindSettings.create_client(self.config)
        try:
            state_client = MindSettings.create_client(self.config)
        except Exception:
            memory_client.close()
            raise

        with self._lock:
            if (
                not self._enabled
                or self._cleaned
                or self.model_runtime is not runtime
            ):
                memory_client.close()
                state_client.close()
                return
            version = runtime.swap(memory_client, state_client, model_name)
            self._ready = True
            self.mood_engine.set_enabled(True)
            self.bond_tracker.set_enabled(True)
        self.log.info("心智模型运行时已热切换: version={} model={}", version, model_name)

    def begin_config_update(self) -> MindModelRuntime | None:
        """暂停未开始的模型调用，避免任务观察到尚未落盘的临时配置。"""
        with self._lock:
            runtime = self.model_runtime
        if runtime is not None:
            runtime.pause_new_acquires()
        return runtime

    @staticmethod
    def end_config_update(runtime: MindModelRuntime | None) -> None:
        if runtime is not None:
            runtime.resume_new_acquires()

    def cleanup(self) -> None:
        with self._generation_lock:
            with self._lock:
                if self._cleaned:
                    return
                self._cleaned = True
                self._generation += 1
                self._enabled = False
                self._ready = False
        self._stop_services(wait_for_memory=False)
        self._join_startup_threads()
        self.mood_engine.cleanup()
        self.bond_tracker.cleanup()

    def _worker_loop(self, worker_queue: queue.Queue[_StateTask | None]) -> None:
        while True:
            task = worker_queue.get()
            if task is None:
                worker_queue.task_done()
                return
            try:
                if not self._task_valid(task.generation):
                    continue
                analyzer = self.state_analyzer
                if analyzer is None:
                    continue
                with self._lock:
                    mood_before = dict(self.mood_engine.load())
                    bond_before = dict(self.bond_tracker.load())
                started = time.monotonic()
                result = analyzer.analyze(task.turns, mood_before, bond_before, task.trace_id)
                with self._lock:
                    if not self.active or task.generation != self._generation:
                        self.log.info("丢弃过期心智任务: {}", task.task_id)
                        continue
                    mood_after = self.mood_engine.update(**result.mood.model_dump())
                    bond_after = self.bond_tracker.update(**result.bond.model_dump())
                self.log.info(
                    "心智状态更新完成: task={} P={:+.2f} A={:.2f} D={:+.2f} W={:.2f} T={:.2f} F={:.2f} H={:.2f} ({:.0f}ms)",
                    task.task_id,
                    mood_after["pleasure"], mood_after["arousal"], mood_after["dominance"],
                    bond_after["warmth"], bond_after["trust"], bond_after["formality"], bond_after["humor"],
                    (time.monotonic() - started) * 1000,
                )
            except MindModelError as exc:
                self.log.error("心智状态模型失败: task={} error={}", task.task_id, exc)
                current_runtime = (
                    exc.runtime_version is not None
                    and analyzer.runtime.is_current(exc.runtime_version)
                )
                self._handle_model_failure(
                    task.generation, task.warn, exc.permanent, current_runtime
                )
            except Exception as exc:
                self.log.opt(exception=True).error("心智状态后台任务异常，已放弃本轮: {}", exc)
            finally:
                worker_queue.task_done()
                if worker_queue.qsize() < self.config.queue_warning_threshold:
                    self._next_queue_warning = self.config.queue_warning_threshold

    def _handle_model_failure(self, generation: int, callback: WarningCallback,
                              permanent: bool, current_runtime: bool = True) -> None:
        if not current_runtime or not self._task_valid(generation):
            return
        self._notify_user_warning(callback)
        if permanent:
            # 用新 generation 一次性失效本次无效配置下的全部排队/在途任务；
            # 修复配置后只处理新提交的任务，不让旧对话延迟污染状态。
            with self._generation_lock:
                with self._lock:
                    if generation != self._generation:
                        return
                    self._generation += 1
                    self._ready = False
                    self.mood_engine.set_enabled(False)
                    self.bond_tracker.set_enabled(False)
            self.log.warning("心智模型配置不可用，已停止心智注入和新任务")

    def _notify_user_warning(
        self, callback: Optional[WarningCallback]
    ) -> None:
        if callback is None:
            return
        now = time.monotonic()
        with self._lock:
            if now < self._next_user_warning_at:
                return
            self._next_user_warning_at = (
                now + self.config.warning_cooldown_seconds
            )
        try:
            callback()
        except Exception as exc:
            self.log.warning("心智异常提示发送失败: {}", exc)

    def _task_valid(self, generation: int) -> bool:
        with self._lock:
            return self.active and generation == self._generation

    def _stop_services(self, *, wait_for_memory: bool) -> None:
        with self._lock:
            worker = self._worker
            memory = self.memory_manager
            analyzer = self.state_analyzer
            runtime = self.model_runtime
            self._worker = None
            self.memory_manager = None
            self.state_analyzer = None
            self.model_runtime = None
            if worker and worker.is_alive():
                self._queue.put(None)
        if memory:
            if wait_for_memory:
                self._cleanup_memory(memory, wait=True)
            else:
                cleanup_done = threading.Event()
                with self._lock:
                    self._retired_memory_cleanups.add(cleanup_done)
                threading.Thread(
                    target=self._cleanup_retired_memory,
                    args=(memory, cleanup_done),
                    name="mind-memory-service-cleanup",
                    daemon=True,
                ).start()
        if runtime:
            runtime.close()
        if worker and worker.is_alive() and worker is not threading.current_thread():
            if not wait_for_memory:
                threading.Thread(
                    target=self._deferred_service_cleanup,
                    args=(worker, analyzer),
                    name="mind-deferred-cleanup",
                    daemon=True,
                ).start()
                return
            worker.join(timeout=self.config.cleanup_timeout_seconds)
            if worker.is_alive():
                self.log.warning("心智状态 Worker 清理超时，资源将在请求结束后延迟释放")
                threading.Thread(
                    target=self._deferred_service_cleanup,
                    args=(worker, analyzer),
                    name="mind-deferred-cleanup",
                    daemon=True,
                ).start()
                return
        self._cleanup_analyzer(analyzer)

    def _cleanup_memory(self, memory: MemoryManager, wait: bool) -> None:
        try:
            memory.cleanup()
            if wait:
                memory.wait_cleanup()
        except Exception as exc:
            self.log.opt(exception=True).error("心智记忆组件清理异常: {}", exc)

    def _cleanup_retired_memory(
        self, memory: MemoryManager, cleanup_done: threading.Event
    ) -> None:
        try:
            self._cleanup_memory(memory, wait=True)
        finally:
            cleanup_done.set()
            with self._lock:
                self._retired_memory_cleanups.discard(cleanup_done)

    def _wait_retired_memory_cleanups(self, generation: int | None = None) -> bool:
        while True:
            with self._lock:
                pending = tuple(self._retired_memory_cleanups)
            if not pending:
                return True
            for cleanup_done in pending:
                while not cleanup_done.wait(timeout=0.1):
                    if generation is not None and not self._startup_current(generation):
                        return False

    def _join_startup_threads(self) -> None:
        deadline = time.monotonic() + self.config.cleanup_timeout_seconds
        current = threading.current_thread()
        with self._lock:
            threads = tuple(
                thread for thread in getattr(self, "_startup_threads", set())
                if thread is not current
            )
        for thread in threads:
            if thread.ident is None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
        alive = [thread.name for thread in threads if thread.is_alive()]
        if alive:
            self.log.warning("心智启动线程清理超时，将由 generation 门禁阻止过期发布: {}", alive)

    def _deferred_service_cleanup(self, worker, analyzer) -> None:
        worker.join()
        self._cleanup_analyzer(analyzer)

    def _cleanup_analyzer(self, analyzer) -> None:
        if analyzer:
            try:
                analyzer.close()
            except Exception as exc:
                self.log.warning("心智模型客户端关闭异常: {}", exc)


def _once(callback: WarningCallback) -> WarningCallback:
    lock = threading.Lock()
    called = False

    def wrapper() -> None:
        nonlocal called
        with lock:
            if called:
                return
            called = True
        callback()

    return wrapper
