"""应用级心智协调器。"""

import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from .config import MindConfig, MindSettings
from .context_builder import format_turns, normalize_turns
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
        self._lock = threading.RLock()
        self._generation = 0
        self._enabled = False
        self._ready = False
        self._cleaned = False
        self.memory_manager: MemoryManager | None = None
        self.state_analyzer: StateAnalyzer | None = None
        self._queue: queue.Queue[_StateTask | None] = queue.Queue()
        self._worker: threading.Thread | None = None
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
                max_tokens=self.max_context_tokens,
            )
        except Exception as exc:
            self.log.opt(exception=True).error("心智上下文整理失败，已放弃本轮后台任务: {}", exc)
            return
        if not turns:
            return

        callback = _once(on_model_failure or (lambda: None))
        generation = self._generation
        memory = self.memory_manager
        if memory:
            try:
                memory.extract_conversation_async(
                    format_turns(turns), trace_id=trace_id,
                    on_model_failure=lambda permanent=False: self._handle_model_failure(
                        generation, callback, permanent
                    ),
                    should_apply=lambda: self._task_valid(generation),
                )
            except Exception as exc:
                self.log.opt(exception=True).error("记忆后台任务提交失败，已跳过本轮: {}", exc)
        if include_state and self.state_analyzer:
            try:
                task = _StateTask(uuid.uuid4().hex[:8], trace_id, generation, turns, callback)
                self._queue.put(task)
                self.log.debug("心智状态任务已提交: {} (queue={})", task.task_id, self._queue.qsize())
            except Exception as exc:
                self.log.opt(exception=True).error("状态后台任务提交失败，已跳过本轮: {}", exc)

    def reset_session_state(self) -> None:
        self.mood_engine.reset_state()
        self.bond_tracker.reset_state()

    def reconfigure(self, enabled: bool) -> None:
        with self._lock:
            self._cleaned = False
            self._generation += 1
            self._enabled = False
            self._ready = False
            self.mood_engine.set_enabled(False)
            self.bond_tracker.set_enabled(False)
        self._stop_services()
        with self._lock:
            self._enabled = enabled
        if not enabled:
            self.log.info("心智系统已关闭，记忆/情绪/羁绊已冻结")
            return
        memory = None
        analyzer = None
        try:
            MindSettings.validate_environment()
            model_name = MindSettings.model_name()
            memory = MemoryManager(
                self.memory_config,
                client=MindSettings.create_client(self.config),
                model_name=model_name,
            )
            analyzer = StateAnalyzer(
                self.config,
                MindSettings.create_client(self.config),
                model_name,
            )
            with self._lock:
                self.memory_manager = memory
                self.state_analyzer = analyzer
                self._ready = True
                self.mood_engine.set_enabled(True)
                self.bond_tracker.set_enabled(True)
                self._queue = queue.Queue()
                worker_queue = self._queue
                self._worker = threading.Thread(
                    target=self._worker_loop, args=(worker_queue,), daemon=True
                )
                self._worker.start()
            self.log.info("心智系统已启用")
        except Exception as exc:
            self.log.warning("心智系统配置或初始化失败，已降级关闭: {}", exc)
            if memory:
                try:
                    memory.cleanup()
                except Exception as cleanup_exc:
                    self.log.warning("心智记忆组件初始化回滚异常: {}", cleanup_exc)
            if analyzer:
                try:
                    analyzer.close()
                except Exception as cleanup_exc:
                    self.log.warning("心智状态组件初始化回滚异常: {}", cleanup_exc)
            with self._lock:
                # 构造后半段（含 Worker 启动）失败时也必须撤销已发布引用和状态开关。
                if self.memory_manager is memory:
                    self.memory_manager = None
                if self.state_analyzer is analyzer:
                    self.state_analyzer = None
                self._worker = None
                self._ready = False
                self.mood_engine.set_enabled(False)
                self.bond_tracker.set_enabled(False)

    def cleanup(self) -> None:
        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
            self._generation += 1
            self._enabled = False
            self._ready = False
        self._stop_services()
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
                if not self._task_valid(task.generation):
                    self.log.info("丢弃过期心智任务: {}", task.task_id)
                    continue
                with self._lock:
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
                self._handle_model_failure(task.generation, task.warn, exc.permanent)
            except Exception as exc:
                self.log.opt(exception=True).error("心智状态后台任务异常，已放弃本轮: {}", exc)
            finally:
                worker_queue.task_done()

    def _handle_model_failure(self, generation: int, callback: WarningCallback,
                              permanent: bool) -> None:
        if not self._task_valid(generation):
            return
        callback()
        if permanent:
            with self._lock:
                self._ready = False
                self.mood_engine.set_enabled(False)
                self.bond_tracker.set_enabled(False)
            self.log.warning("心智模型配置不可用，已停止心智注入和新任务")

    def _task_valid(self, generation: int) -> bool:
        with self._lock:
            return self.active and generation == self._generation

    def _stop_services(self) -> None:
        with self._lock:
            worker = self._worker
            memory = self.memory_manager
            analyzer = self.state_analyzer
            self._worker = None
            self.memory_manager = None
            self.state_analyzer = None
            if worker and worker.is_alive():
                self._queue.put(None)
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=self.config.cleanup_timeout_seconds)
            if worker.is_alive():
                self.log.warning("心智状态 Worker 清理超时，将丢弃未完成结果")
        if memory:
            memory.cleanup()
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
