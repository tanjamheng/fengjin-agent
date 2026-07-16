"""记忆写入与冲突消解"""

import os
import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from .config import MemoryConfig
from .storage import MemoryStorage
from ..utils.logger import get_logger


class MemoryWriter:
    """记忆写入器

    核心机制：
    - Queue + Writer 线程：串行化所有 ChromaDB 写入，防止并发锁
    - 三级阈值路由：dedup → merge → insert
    - core 记忆保护：low-importance 不能修改 core 记忆
    """

    def __init__(self, config: MemoryConfig, client: OpenAI,
                 model: str, storage: MemoryStorage):
        self.config = config
        self.client = client
        self.model = model
        self.storage = storage
        self.log = get_logger("memory_writer")
        self._core_path = Path(config.core_file)
        if not self._core_path.is_absolute():
            from ..utils.helpers import get_project_root
            self._core_path = get_project_root() / self._core_path
        try:
            merge_prompt = Path(config.merge.prompt_file)
            if not merge_prompt.is_absolute():
                from ..utils.helpers import get_project_root
                merge_prompt = get_project_root() / merge_prompt
            self._merge_prompt_template = merge_prompt.read_text(
                encoding="utf-8"
            )
        except Exception as e:
            self.log.warning("记忆合并 prompt 文件读取失败，使用内嵌默认模板: {}", e)
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

        self._queue: queue.Queue = queue.Queue(maxsize=300)
        self._dump_path = Path(config.chroma.persist_directory) / "pending_facts.json"
        self._running = True
        self._stopping = False  # stop() 已调用标志，防止 _checkpoint 覆盖 _dump_pending
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()
        self._replay_pending()

    def write(self, facts: list[dict], should_apply=None) -> None:
        """将过滤后的事实加入写入队列（非阻塞），并立即持久化队列快照

        入队后立即写 pending_facts.json，保证 LLM 已决定的 facts 不因崩溃丢失。
        ChromaDB 向量去重保证重放幂等。
        """
        for fact in facts:
            self._queue.put((fact, should_apply))
        self._checkpoint()

    def stop(self) -> None:
        """停止写入线程，持久化剩余任务以防数据丢失"""
        self._running = False
        self._stopping = True
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            self.log.warning("写入线程超时未退出，剩余队列任务将被持久化")
        # 无论线程是否正常退出，都将队列剩余任务持久化
        self._dump_pending()

    def _writer_loop(self) -> None:
        """后台写入线程，用 poll 方式串行处理所有写入任务"""
        while self._running:
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                fact, should_apply = _unpack_item(item)
                if not _can_apply(should_apply):
                    self.log.info("丢弃已失效的记忆写入任务")
                    continue
                self._process_fact(fact, should_apply)
            except Exception as e:
                self.log.error("记忆写入失败: {}", e)
            finally:
                self._queue.task_done()

    def _checkpoint(self) -> None:
        """非破坏性快照：将队列当前内容原子写入 pending_facts.json

        和 _dump_pending() 不同，不排空队列——writer 线程正在并行处理。
        用 queue.mutex 安全读取内部 deque 快照。
        """
        import json
        with self._queue.mutex:
            items = [
                fact for fact, should_apply in map(_unpack_item, self._queue.queue)
                if _can_apply(should_apply)
            ]
        if not items:
            # 队列已空，清理残留文件
            # 但如果正在停止，_dump_pending 已经写了文件，不能删除
            if self._dump_path.exists() and not self._stopping:
                try:
                    self._dump_path.unlink()
                except OSError:
                    pass
            return

        self._dump_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写 .tmp 再 os.replace()（红线7）
        tmp_path = str(self._dump_path) + ".tmp"
        Path(tmp_path).write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, str(self._dump_path))

    def _process_fact(self, fact: dict, should_apply=None) -> None:
        """处理单条事实：路由到 insert 或 merge，成功后更新快照"""
        if not _can_apply(should_apply):
            return
        is_core = fact["importance"] == "high"
        conflict_distance = self.config.thresholds.conflict_distance

        results = self.storage.query(
            text=fact["content"],
            n_results=1,
            where={"is_core": 1 if is_core else 0}
        )

        if not results["ids"][0]:
            if not _can_apply(should_apply):
                return
            self._insert(fact, is_core)
            if is_core:
                self._refresh_core_file()
        else:
            distance = results["distances"][0][0]
            if not _can_apply(should_apply):
                return
            if distance >= conflict_distance:
                self._insert(fact, is_core)
            else:
                old_id = results["ids"][0][0]
                self._resolve_conflict(old_id, fact, is_core, should_apply)

            if is_core:
                self._refresh_core_file()

        # 处理完成后更新快照，从文件中移除已处理的事实
        self._checkpoint()

    def _insert(self, fact: dict, is_core: bool) -> None:
        """插入新记忆"""
        memory_id = str(uuid.uuid4())
        self.storage.add(
            memory_id=memory_id,
            content=fact["content"],
            is_core=is_core,
            memory_type=fact["type"]
        )

    def _resolve_conflict(self, old_id: str, fact: dict, is_core: bool,
                          should_apply=None) -> None:
        """冲突消解。LLM 合并失败时降级为直接插入新事实。"""
        if not _can_apply(should_apply):
            return
        old_result = self.storage.get(ids=[old_id])
        old_content = old_result["documents"][0]
        old_meta = old_result["metadatas"][0]

        # core 保护：low-importance 不能修改 core 记忆
        if old_meta["is_core"] and not is_core:
            if not _can_apply(should_apply):
                return
            self._insert(fact, is_core=False)
            return

        try:
            merged = self._llm_merge(old_content, fact["content"])
        except Exception as e:
            self.log.error("LLM记忆合并失败，降级为直接插入: {}", e)
            if not _can_apply(should_apply):
                return
            self._insert(fact, is_core)
            return

        if not _can_apply(should_apply):
            return
        if merged == "NO_MERGE":
            self._insert(fact, is_core)
            return

        old_meta["updated_at"] = datetime.now().isoformat()
        self.storage.upsert(memory_id=old_id, content=merged, metadata=old_meta)

    def _llm_merge(self, old_memory: str, new_fact: str) -> str:
        """调用小模型合并两条记忆"""
        prompt = self._merge_prompt_template.replace(
            "{old_memory}", old_memory
        ).replace(
            "{new_fact}", new_fact
        )

        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.config.merge.max_tokens,
            messages=[{"role": "user", "content": prompt}],
            timeout=30.0,
        )
        return response.choices[0].message.content.strip()

    def _refresh_core_file(self) -> None:
        """从 ChromaDB 生成 core_memory.md"""
        results = self.storage.get_by_metadata(
            where={"is_core": 1},
            include=["documents", "metadatas"]
        )
        text = "# 灰宝的档案\n"
        if results["documents"]:
            paired = list(zip(results["documents"], results["metadatas"]))
            paired.sort(key=lambda x: x[1].get("created_at", ""))
            text += "\n".join(f"- {doc}" for doc, _ in paired)

        self._core_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写 .tmp 再 os.replace()，防止中途崩溃损坏文件（红线7）
        tmp_path = str(self._core_path) + ".tmp"
        Path(tmp_path).write_text(text, encoding="utf-8")
        os.replace(tmp_path, self._core_path)

    def _replay_pending(self) -> None:
        """启动时回放上次异常退出遗留的 pending_facts.json"""
        import json
        dump_path = Path(self.config.chroma.persist_directory) / "pending_facts.json"
        if not dump_path.exists():
            return
        try:
            facts = json.loads(dump_path.read_text(encoding="utf-8"))
            dump_path.unlink()
            if facts:
                self.log.info("回放 {} 条遗留未处理记忆", len(facts))
                for fact in facts:
                    fact.pop("_dumped_at", None)
                    self._queue.put((fact, None))
        except (json.JSONDecodeError, OSError) as e:
            self.log.warning("pending_facts.json 读取失败，已跳过: {}", e)

    def _dump_pending(self) -> None:
        """将队列中剩余任务持久化到临时文件，下次启动可回放"""
        facts = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            fact, should_apply = _unpack_item(item)
            if _can_apply(should_apply):
                facts.append(fact)

        if not facts:
            dump_path = Path(self.config.chroma.persist_directory) / "pending_facts.json"
            if dump_path.exists():
                try:
                    dump_path.unlink()
                except OSError as exc:
                    self.log.warning("清理已失效记忆快照失败: {}", exc)
            return

        import json
        from datetime import datetime
        dump_path = Path(self.config.chroma.persist_directory) / "pending_facts.json"
        dump_path.parent.mkdir(parents=True, exist_ok=True)

        existing = []
        if dump_path.exists():
            try:
                existing = json.loads(dump_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.log.warning("pending_facts.json 损坏，将丢弃并重建")

        for fact in facts:
            fact["_dumped_at"] = datetime.now().isoformat()
        existing.extend(facts)

        # 原子写入：先写 .tmp 再 os.replace()，防止中途崩溃损坏文件（红线7）
        tmp_path = str(dump_path) + ".tmp"
        tmp_path_obj = Path(tmp_path)
        tmp_path_obj.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        os.replace(tmp_path, str(dump_path))
        self.log.info("已持久化 {} 条未处理记忆到 {}", len(facts), dump_path)


def _unpack_item(item) -> tuple[dict, object]:
    """兼容旧测试或调用直接向队列放入 fact dict。"""
    if isinstance(item, tuple) and len(item) == 2:
        return item
    return item, None


def _can_apply(should_apply) -> bool:
    if should_apply is None:
        return True
    try:
        return bool(should_apply())
    except Exception:
        return False
