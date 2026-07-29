"""记忆写入、冲突消解与崩溃恢复。"""

import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from .config import MemoryConfig
from .storage import MemoryStorage
from .temporal import canonical_event_date
from ..mind.model_runtime import MindModelRuntime
from ..utils.helpers import get_project_root
from ..utils.logger import get_logger


class MemoryWriter:
    """通过单线程串行写入 ChromaDB，并用 WAL 保证待写事实可恢复。"""

    _WAL_VERSION = 1

    def __init__(self, config: MemoryConfig, client: OpenAI | None,
                 model: str, storage: MemoryStorage, max_retries: int = 3,
                 *, runtime: MindModelRuntime | None = None):
        self.config = config
        if runtime is None:
            if client is None:
                raise ValueError("MemoryWriter 需要 client 或 runtime")
            runtime = MindModelRuntime.single_client(client, model)
        self.runtime = runtime
        self.storage = storage
        self.max_retries = max_retries
        self.log = get_logger("memory_writer")

        self._core_path = Path(config.core_file)
        if not self._core_path.is_absolute():
            self._core_path = get_project_root() / self._core_path

        try:
            merge_prompt = Path(config.merge.prompt_file)
            if not merge_prompt.is_absolute():
                merge_prompt = get_project_root() / merge_prompt
            self._merge_prompt_template = merge_prompt.read_text(encoding="utf-8")
        except Exception as exc:
            self.log.warning("记忆合并 prompt 文件读取失败，使用内嵌默认模板: {}", exc)
            self._merge_prompt_template = (
                "请将以下两条关于用户的信息合并为一条简洁的事实：\n"
                "旧记忆：{old_memory}\n新事实：{new_fact}\n"
                "规则：\n"
                "1. 两者矛盾 → 用新事实替换旧记忆（用户的情况可能改变了）\n"
                "2. 两者互补 → 合并为一条更完整的事实\n"
                "3. 两者描述同一事 → 保留更具体、信息量更大的版本\n"
                "4. 两者是独立不相关的事实 → 返回 NO_MERGE\n"
                "5. 两者无关 → 返回 NO_MERGE"
            )

        persist_dir = Path(config.chroma.persist_directory)
        if not persist_dir.is_absolute():
            persist_dir = get_project_root() / persist_dir
        self._dump_path = persist_dir / "pending_facts.json"
        self._wal_lock = threading.RLock()
        self._pending: dict[str, dict] = {}
        self._callbacks: dict[str, object] = {}
        # 事实已经先落 WAL；Queue 只承载 task_id，不再用阻塞式上限制造清理死锁。
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._running = True
        self._stopping = False

        # 启动时同步一次现有 Core，旧记录无需重嵌入即可应用新容量策略。
        self._enforce_core_capacity()
        self._refresh_core_file()

        # 回放必须在线程启动前完成；即使 WAL 有坏项，构造失败也不能遗留线程。
        self._replay_pending()
        self._thread = threading.Thread(
            target=self._writer_loop,
            name="mind-memory-writer",
            daemon=True,
        )
        self._thread.start()

    def write(self, facts: list[dict], should_apply=None) -> None:
        """先将事实原子写入 WAL，再交给 Writer 线程，杜绝入队后的崩溃窗口。"""
        task_ids: list[str] = []
        with self._wal_lock:
            for fact in facts:
                task_id = uuid.uuid4().hex
                self._pending[task_id] = {
                    "task_id": task_id,
                    "fact": dict(fact),
                    "stage": "storage",
                }
                self._callbacks[task_id] = should_apply
                task_ids.append(task_id)
            self._checkpoint_locked()

        for task_id in task_ids:
            self._queue.put(task_id)

    def stop(self) -> None:
        """停止线程；未确认完成的任务始终保留在 WAL，供下次启动回放。"""
        if self._stopping:
            return
        self._stopping = True
        self._running = False
        try:
            with self._wal_lock:
                # 热更新/关闭后旧 generation 不得在下次启动被当成有效 WAL 重放。
                invalid_ids = [
                    task_id for task_id, callback in self._callbacks.items()
                    if callback is not None and not _can_apply(callback)
                ]
                for task_id in invalid_ids:
                    self._pending.pop(task_id, None)
                    self._callbacks.pop(task_id, None)
                self._checkpoint_locked()
        except Exception as exc:
            self.log.opt(exception=True).error("停止 Writer 时更新 WAL 失败: {}", exc)
        finally:
            # 即使 WAL I/O 失败也必须唤醒 Worker，不能让延迟清理永久等待。
            self._queue.put(None)
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            self.log.warning("写入线程超时未退出，未确认任务已保留在 WAL")
        try:
            self._checkpoint()
        except Exception as exc:
            self.log.opt(exception=True).error("Writer 停止后的 WAL 快照失败: {}", exc)

    def _writer_loop(self) -> None:
        while self._running:
            try:
                task_id = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if task_id is None:
                    return
                with self._wal_lock:
                    record = self._pending.get(task_id)
                    should_apply = self._callbacks.get(task_id)
                if record is None:
                    continue
                if not _can_apply(should_apply):
                    self.log.info("丢弃已失效的记忆写入任务: {}", task_id[:8])
                    self._ack(task_id)
                    continue
                self._process_record(record, should_apply)
                self._ack(task_id)
            except Exception as exc:
                # 不确认失败任务：它仍在 WAL 中，下次启动可以继续。
                self.log.opt(exception=True).error(
                    "记忆写入失败，任务保留待下次恢复: task={} error={}",
                    str(task_id)[:8], exc,
                )
            finally:
                self._queue.task_done()

    def _process_record(self, record: dict, should_apply=None) -> None:
        task_id = record["task_id"]
        fact = record["fact"]
        is_core = fact["importance"] == "high"
        if record.get("stage") != "core_file":
            applied = self._apply_fact_storage(
                fact,
                should_apply=should_apply,
                memory_id=task_id,
            )
            if not applied:
                return
            if is_core:
                # Chroma 已提交后先记录阶段；若刷新文件时崩溃，重启只重建派生文件。
                with self._wal_lock:
                    current = self._pending.get(task_id)
                    if current is not None:
                        current["stage"] = "core_file"
                        self._checkpoint_locked()
        if is_core:
            self._enforce_core_capacity()
            self._refresh_core_file()

    def _process_fact(self, fact: dict, should_apply=None) -> None:
        """兼容评测和旧调用的同步入口。"""
        if not _can_apply(should_apply):
            return
        applied = self._apply_fact_storage(fact, should_apply=should_apply)
        if not applied:
            return
        if fact["importance"] == "high":
            self._enforce_core_capacity()
            self._refresh_core_file()

    def _apply_fact_storage(self, fact: dict, should_apply=None,
                            memory_id: str | None = None) -> bool:
        if not _can_apply(should_apply):
            return False
        is_core = fact["importance"] == "high"
        results = self.storage.query(
            text=fact["content"],
            n_results=self.config.thresholds.conflict_candidates,
            where=None,
        )
        if not _can_apply(should_apply):
            return False

        candidates = _unpack_query_candidates(results)
        if not candidates:
            return self._insert(
                fact, is_core, memory_id=memory_id, should_apply=should_apply
            )

        duplicates = [
            candidate for candidate in candidates
            if (
                candidate["distance"] < self.config.thresholds.dedup_distance
                and not _is_distinct_temporal_occurrence(
                    fact, candidate["metadata"]
                )
            )
        ]
        if duplicates:
            if (
                not is_core
                or any(item["metadata"].get("is_core", 0) for item in duplicates)
            ):
                return True
            duplicate = duplicates[0]
            old_id = duplicate["id"]
            old_result = self.storage.get(ids=[old_id])
            old_meta = old_result["metadatas"][0]
            now = datetime.now().isoformat()
            old_meta["is_core"] = 1
            old_meta["importance"] = "high"
            old_meta["updated_at"] = now
            old_meta["core_touched_at"] = now
            _apply_temporal_metadata(old_meta, fact)
            if self._is_protected(fact["content"]):
                old_meta["protected"] = 1
            return _commit_if_valid(
                should_apply,
                lambda: self.storage.update_metadata(old_id, old_meta),
            )

        conflicts = [
            candidate for candidate in candidates
            if (
                candidate["distance"] < self.config.thresholds.conflict_distance
                and not _is_distinct_temporal_occurrence(
                    fact, candidate["metadata"]
                )
            )
        ]
        if not conflicts:
            return self._insert(
                fact, is_core, memory_id=memory_id, should_apply=should_apply
            )
        matching_importance = [
            candidate for candidate in conflicts
            if candidate["metadata"].get(
                "importance",
                "high" if candidate["metadata"].get("is_core", 0) else "low",
            ) == fact["importance"]
        ]
        old_id = (matching_importance or conflicts)[0]["id"]
        return self._resolve_conflict(
                old_id, fact, is_core, should_apply, memory_id=memory_id
            )

    def _insert(self, fact: dict, is_core: bool,
                memory_id: str | None = None, should_apply=None) -> bool:
        return _commit_if_valid(
            should_apply,
            lambda: self.storage.add(
                memory_id=memory_id or str(uuid.uuid4()),
                content=fact["content"],
                is_core=is_core,
                memory_type=fact["type"],
                protected=is_core and self._is_protected(fact["content"]),
                importance=fact["importance"],
                source_timestamp=fact.get("source_timestamp"),
                event_time=fact.get("event_time"),
                time_scope=fact.get("time_scope", "timeless"),
            ),
        )

    def _resolve_conflict(self, old_id: str, fact: dict, is_core: bool,
                          should_apply=None,
                          memory_id: str | None = None) -> bool:
        """冲突合并最终失败时仅记日志并放弃本条更新（不触发前端提示）。"""
        if not _can_apply(should_apply):
            return False
        old_result = self.storage.get(ids=[old_id])
        old_content = old_result["documents"][0]
        old_meta = old_result["metadatas"][0]
        if old_meta["is_core"] and not is_core:
            return self._insert(
                fact, is_core=False, memory_id=memory_id,
                should_apply=should_apply,
            )

        try:
            merged = self._llm_merge(old_content, fact["content"])
        except Exception as exc:
            self.log.error("LLM记忆合并失败，已放弃本条更新: {}", exc)
            return True
        if not _can_apply(should_apply):
            return False
        if merged == "NO_MERGE":
            return self._insert(
                fact, is_core, memory_id=memory_id,
                should_apply=should_apply,
            )
        now = datetime.now().isoformat()
        old_meta["updated_at"] = now
        _apply_temporal_metadata(old_meta, fact)
        if is_core:
            old_meta["is_core"] = 1
            old_meta["importance"] = "high"
            old_meta["core_touched_at"] = now
            if self._is_protected(fact["content"]):
                old_meta["protected"] = 1
        return _commit_if_valid(
            should_apply,
            lambda: self.storage.upsert(
                memory_id=old_id, content=merged, metadata=old_meta
            ),
        )

    def _llm_merge(self, old_memory: str, new_fact: str) -> str:
        runtime = getattr(self, "runtime", None)
        if runtime is None:
            return self._llm_merge_with_client(
                old_memory, new_fact, self.client, self.model
            )
        with runtime.acquire("memory") as lease:
            return self._llm_merge_with_client(
                old_memory, new_fact, lease.client, lease.model
            )

    def _llm_merge_with_client(self, old_memory: str, new_fact: str,
                               client, model: str) -> str:
        prompt = self._merge_prompt_template.replace(
            "{old_memory}", old_memory
        ).replace("{new_fact}", new_fact)
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response = client.chat.completions.create(
                    model=model,
                    max_tokens=self.config.merge.max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.choices[0].message.content.strip()
            except Exception as exc:
                last_error = exc
                if getattr(exc, "status_code", None) in (401, 403, 404):
                    raise
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 4))
        if last_error is not None:
            raise last_error
        raise RuntimeError("记忆合并模型未返回结果")

    def _is_protected(self, content: str) -> bool:
        patterns = self.config.core_capacity.protected_patterns
        return any(re.search(pattern, content) for pattern in patterns)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """与上下文模块口径一致的轻量估算：ASCII≈0.3 token，中文≈1 token。"""
        ascii_chars = sum(1 for char in text if ord(char) < 128)
        return max(1, int(ascii_chars * 0.3 + (len(text) - ascii_chars)))

    def _enforce_core_capacity(self) -> None:
        """超限时把最久未触达的非保护 Core 降为普通记忆，只更新 Metadata。"""
        capacity = self.config.core_capacity
        if not capacity.enabled:
            return

        results = self.storage.get_by_metadata(
            where={"is_core": 1}, include=["documents", "metadatas"]
        )
        records = []
        for memory_id, content, raw_meta in zip(
            results["ids"], results["documents"], results["metadatas"]
        ):
            metadata = dict(raw_meta)
            metadata_changed = False
            if "importance" not in metadata:
                metadata["importance"] = "high"
                metadata_changed = True
            if "core_touched_at" not in metadata:
                metadata["core_touched_at"] = (
                    metadata.get("updated_at")
                    or metadata.get("created_at", "")
                )
                metadata_changed = True
            protected = bool(metadata.get("protected", 0))
            if not protected and self._is_protected(content):
                protected = True
                metadata["protected"] = 1
                metadata_changed = True
            if metadata_changed:
                self.storage.update_metadata(memory_id, metadata)
            records.append({
                "id": memory_id,
                "content": content,
                "metadata": metadata,
                "protected": protected,
                "tokens": self._estimate_tokens(
                    _format_core_memory_line(content, metadata)
                ),
            })

        records.sort(
            key=lambda item: item["metadata"].get("core_touched_at", "")
        )
        total_tokens = sum(item["tokens"] for item in records)
        candidates = [item for item in records if not item["protected"]]
        demoted = 0
        while (
            len(records) > capacity.max_items
            or total_tokens > capacity.max_tokens
        ) and candidates:
            item = candidates.pop(0)
            metadata = dict(item["metadata"])
            metadata["is_core"] = 0
            metadata["demoted_at"] = datetime.now().isoformat()
            self.storage.update_metadata(item["id"], metadata)
            records.remove(item)
            total_tokens -= item["tokens"]
            demoted += 1

        if demoted:
            self.log.info(
                "Core 容量整理完成: 降级 {} 条，保留 {} 条 / 约 {} tokens",
                demoted, len(records), total_tokens,
            )
        if (
            len(records) > capacity.max_items
            or total_tokens > capacity.max_tokens
        ):
            self.log.warning(
                "受保护 Core 已超过容量，未强制降级: {} 条 / 约 {} tokens",
                len(records), total_tokens,
            )

    def _refresh_core_file(self) -> None:
        results = self.storage.get_by_metadata(
            where={"is_core": 1}, include=["documents", "metadatas"]
        )
        text = "# 灰宝的档案\n"
        if results["documents"]:
            paired = list(zip(results["documents"], results["metadatas"]))
            paired.sort(key=lambda item: item[1].get("created_at", ""))
            text += "\n".join(
                _format_core_memory_line(doc, metadata)
                for doc, metadata in paired
            )
        self._core_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._core_path.with_suffix(self._core_path.suffix + ".tmp")
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, self._core_path)

    def _ack(self, task_id: str) -> None:
        with self._wal_lock:
            record = self._pending.get(task_id)
            if record is None:
                return
            # 两阶段确认：先把磁盘记录标成 done，再尝试物理删除。
            # 即使最终 unlink/replace 失败，重启也只会跳过 done，不会重复执行。
            record["stage"] = "done"
            self._checkpoint_locked()
            self._pending.pop(task_id, None)
            self._callbacks.pop(task_id, None)
            self._checkpoint_locked()

    def _checkpoint(self) -> None:
        with self._wal_lock:
            self._checkpoint_locked()

    def _checkpoint_locked(self) -> None:
        if not self._pending:
            if self._dump_path.exists():
                try:
                    self._dump_path.unlink()
                except OSError as exc:
                    self.log.warning("清理空记忆 WAL 失败: {}", exc)
            return
        payload = {
            "version": self._WAL_VERSION,
            "tasks": list(self._pending.values()),
        }
        self._dump_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._dump_path.with_suffix(self._dump_path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self._dump_path)

    def _replay_pending(self) -> None:
        if not self._dump_path.exists():
            return
        try:
            payload = json.loads(self._dump_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self.log.warning("pending_facts.json 读取失败，保留原文件等待人工处理: {}", exc)
            return

        raw_tasks = payload.get("tasks", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_tasks, list):
            self.log.warning("pending_facts.json 结构无效，保留原文件等待人工处理")
            return

        skipped = 0
        for item in raw_tasks:
            record = self._normalize_wal_record(item)
            if record is None:
                skipped += 1
                continue
            if record["stage"] == "done":
                continue
            task_id = record["task_id"]
            self._pending[task_id] = record
            self._callbacks[task_id] = None
            self._queue.put(task_id)
        if skipped:
            self.log.warning("记忆 WAL 中 {} 个坏项已跳过", skipped)
        if self._pending:
            self.log.info("回放 {} 条遗留未处理记忆", len(self._pending))
            # 将旧版列表格式迁移为带阶段的新格式，但绝不提前删除 WAL。
            self._checkpoint_locked()

    @staticmethod
    def _normalize_wal_record(item) -> dict | None:
        if not isinstance(item, dict):
            return None
        if isinstance(item.get("fact"), dict):
            fact = dict(item["fact"])
            task_id = str(item.get("task_id") or uuid.uuid4().hex)
            stage = item.get("stage", "storage")
        else:
            fact = dict(item)
            fact.pop("_dumped_at", None)
            task_id = uuid.uuid4().hex
            stage = "storage"
        if not all(isinstance(fact.get(key), str) and fact.get(key)
                   for key in ("content", "type", "importance")):
            return None
        if stage not in ("storage", "core_file", "done"):
            stage = "storage"
        return {"task_id": task_id, "fact": fact, "stage": stage}

    def _dump_pending(self) -> None:
        """兼容旧清理调用；当前 WAL 持续维护，无需排空 Queue。"""
        self._checkpoint()


def _unpack_item(item) -> tuple[dict, object]:
    """兼容外部旧测试辅助函数。"""
    if isinstance(item, tuple) and len(item) == 2:
        return item
    return item, None


def _apply_temporal_metadata(metadata: dict, fact: dict) -> None:
    """把新事实的时间语义同步到更新后的记忆 Metadata。"""
    if "time_scope" in fact:
        metadata["time_scope"] = fact["time_scope"]
        if fact.get("event_time"):
            metadata["event_time"] = fact["event_time"]
        else:
            metadata.pop("event_time", None)
    if fact.get("source_timestamp"):
        metadata["source_timestamp"] = fact["source_timestamp"]


def _format_core_memory_line(document: str, metadata: dict) -> str:
    """在 Core 派生视图中显示来源日期，并给旧相对时间记忆补充解释锚点。"""
    source_time = metadata.get("source_timestamp") or metadata.get("created_at", "")
    source_date = str(source_time).split("T", 1)[0] if source_time else ""
    labels = []
    if source_date:
        labels.append(f"记录于 {source_date}")
    event_time = metadata.get("event_time")
    if event_time:
        labels.append(f"事件日期 {event_time}")
    scope_labels = {
        "recurring": "周期性",
        "temporary": "阶段性",
        "event": "一次性事件",
    }
    scope_label = scope_labels.get(metadata.get("time_scope"))
    if scope_label:
        labels.append(scope_label)
    if re.search(r"今天|今日|昨天|昨日|明天|明日|前天|后天", document):
        labels.append("正文相对时间以记录日期为准")
    prefix = f"[{'；'.join(labels)}] " if labels else ""
    return f"- {prefix}{document}"


def _is_distinct_temporal_occurrence(fact: dict, metadata: dict) -> bool:
    """限制带时间事实只与同类、同一次事件参与去重或合并。"""
    new_scope = fact.get("time_scope", "timeless")
    old_scope = metadata.get("time_scope", "timeless")
    if new_scope != old_scope and (
        new_scope != "timeless" or old_scope != "timeless"
    ):
        return True
    if new_scope in {"temporary", "event"}:
        new_time = canonical_event_date(fact.get("event_time"))
        old_time = canonical_event_date(metadata.get("event_time"))
        return bool(new_time or old_time) and new_time != old_time
    return False


def _unpack_query_candidates(results: dict) -> list[dict]:
    """把 Chroma 单查询结果整理为按距离排序的候选列表。"""
    ids = (results.get("ids") or [[]])[0]
    distances = (results.get("distances") or [[]])[0]
    metadatas = (results.get("metadatas") or [[]])[0]
    return [
        {
            "id": memory_id,
            "distance": distances[index],
            "metadata": metadatas[index] if index < len(metadatas) else {},
        }
        for index, memory_id in enumerate(ids)
        if index < len(distances)
    ]


def _can_apply(should_apply) -> bool:
    if should_apply is None:
        return True
    try:
        return bool(should_apply())
    except Exception:
        return False


def _commit_if_valid(should_apply, action) -> bool:
    """若 gate 支持原子 commit，则在 generation 锁内完成最终持久化。"""
    if should_apply is not None and hasattr(should_apply, "commit"):
        try:
            return bool(should_apply.commit(action))
        except Exception:
            raise
    if not _can_apply(should_apply):
        return False
    action()
    return True
