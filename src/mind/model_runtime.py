"""心智模型客户端热切换：任务开始时取快照，旧客户端延迟释放。"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

from ..utils.logger import get_logger

Channel = Literal["memory", "state"]


@dataclass
class _RuntimeSlot:
    memory_client: object
    state_client: object
    model: str
    version: int
    users: int = 0
    retired: bool = False


class MindModelLease:
    """一次模型调用持有的不可变运行时快照。"""

    def __init__(self, runtime: "MindModelRuntime", slot: _RuntimeSlot,
                 channel: Channel):
        self._runtime = runtime
        self._slot = slot
        self.client = (
            slot.memory_client if channel == "memory" else slot.state_client
        )
        self.model = slot.model
        self.version = slot.version
        self._released = False

    def __enter__(self) -> "MindModelLease":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._runtime._release(self._slot)


class MindModelRuntime:
    """原子切换记忆/状态客户端，并为在途调用保留旧客户端。"""

    def __init__(self, memory_client, state_client, model: str):
        self._lock = threading.RLock()
        self._acquire_gate = threading.Condition(self._lock)
        self._acquires_paused = 0
        self._current = _RuntimeSlot(memory_client, state_client, model, 1)
        self._closed = False
        self.log = get_logger("mind_runtime")

    @classmethod
    def single_client(cls, client, model: str) -> "MindModelRuntime":
        """供独立组件和测试使用；两个通道共享同一客户端。"""
        return cls(client, client, model)

    @property
    def current_version(self) -> int:
        with self._lock:
            return self._current.version

    def is_current(self, version: int) -> bool:
        with self._lock:
            return not self._closed and self._current.version == version

    def acquire(self, channel: Channel) -> MindModelLease:
        with self._acquire_gate:
            while self._acquires_paused and not self._closed:
                self._acquire_gate.wait()
            if self._closed:
                raise RuntimeError("心智模型运行时已关闭")
            slot = self._current
            slot.users += 1
        return MindModelLease(self, slot, channel)

    def pause_new_acquires(self) -> None:
        """配置事务期间暂停尚未开始的模型调用；在途租约不受影响。"""
        with self._acquire_gate:
            if self._closed:
                return
            self._acquires_paused += 1

    def resume_new_acquires(self) -> None:
        with self._acquire_gate:
            if self._acquires_paused > 0:
                self._acquires_paused -= 1
            if self._acquires_paused == 0:
                self._acquire_gate.notify_all()

    def swap(self, memory_client, state_client, model: str) -> int:
        """切换当前配置；已获取旧快照的调用继续运行。"""
        to_close: list[object] = []
        with self._lock:
            was_closed = self._closed
            if was_closed:
                to_close = [memory_client, state_client]
                version = self._current.version
            else:
                old = self._current
                old.retired = True
                self._current = _RuntimeSlot(
                    memory_client,
                    state_client,
                    model,
                    old.version + 1,
                )
                version = self._current.version
                if old.users == 0:
                    to_close = self._slot_clients(old)
        self._close_clients(to_close)
        if was_closed:
            raise RuntimeError("心智模型运行时已关闭")
        return version

    def close(self) -> None:
        to_close: list[object] = []
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._acquire_gate.notify_all()
            current = self._current
            current.retired = True
            if current.users == 0:
                to_close = self._slot_clients(current)
        self._close_clients(to_close)

    def _release(self, slot: _RuntimeSlot) -> None:
        to_close: list[object] = []
        with self._lock:
            slot.users = max(0, slot.users - 1)
            if slot.retired and slot.users == 0:
                to_close = self._slot_clients(slot)
        self._close_clients(to_close)

    @staticmethod
    def _slot_clients(slot: _RuntimeSlot) -> list[object]:
        if slot.memory_client is slot.state_client:
            return [slot.memory_client]
        return [slot.memory_client, slot.state_client]

    def _close_clients(self, clients: list[object]) -> None:
        for client in clients:
            try:
                client.close()
            except Exception as exc:
                self.log.warning("关闭旧心智模型客户端失败: {}", exc)
