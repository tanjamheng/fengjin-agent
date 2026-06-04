"""记忆写入与冲突消解"""

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
        self._merge_prompt_template = Path(config.merge.prompt_file).read_text(
            encoding="utf-8"
        )

        self._queue: queue.Queue = queue.Queue()
        self._running = True
        self.log = get_logger("memory_writer")
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()

    def write(self, facts: list[dict]) -> None:
        """将过滤后的事实加入写入队列（非阻塞）"""
        for fact in facts:
            self._queue.put(fact)

    def stop(self) -> None:
        """停止写入线程"""
        self._running = False
        pending = self._queue.qsize()
        self._queue.put(None)
        self._thread.join(timeout=30)
        if self._thread.is_alive():
            remaining = self._queue.qsize()
            self.log.warning(f"写入线程超时未退出，丢弃队列中约 {remaining} 条任务（原队列 {pending} 条）")

    def _writer_loop(self) -> None:
        """后台写入线程，串行处理所有写入任务"""
        while self._running:
            item = self._queue.get()
            if item is None:
                break
            try:
                self._process_fact(item)
            except Exception as e:
                self.log.error(f"记忆写入失败: {e}")
            finally:
                self._queue.task_done()

    def _process_fact(self, fact: dict) -> None:
        """处理单条事实：路由到 insert 或 merge"""
        is_core = fact["importance"] == "high"
        conflict_distance = self.config.thresholds.conflict_distance

        results = self.storage.query(
            text=fact["content"],
            n_results=1,
            where={"is_core": 1 if is_core else 0}
        )

        if not results["ids"][0]:
            self._insert(fact, is_core)
            if is_core:
                self._refresh_core_file()
            return

        distance = results["distances"][0][0]
        if distance >= conflict_distance:
            self._insert(fact, is_core)
        else:
            old_id = results["ids"][0][0]
            self._resolve_conflict(old_id, fact, is_core)

        if is_core:
            self._refresh_core_file()

    def _insert(self, fact: dict, is_core: bool) -> None:
        """插入新记忆"""
        memory_id = str(uuid.uuid4())
        self.storage.add(
            memory_id=memory_id,
            content=fact["content"],
            is_core=is_core,
            memory_type=fact["type"]
        )

    def _resolve_conflict(self, old_id: str, fact: dict, is_core: bool) -> None:
        """冲突消解"""
        old_result = self.storage.get(ids=[old_id])
        old_content = old_result["documents"][0]
        old_meta = old_result["metadatas"][0]

        # core 保护：low-importance 不能修改 core 记忆
        if old_meta["is_core"] and not is_core:
            self._insert(fact, is_core=False)
            return

        merged = self._llm_merge(old_content, fact["content"])

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
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()

    def _refresh_core_file(self) -> None:
        """从 ChromaDB 生成 core_memory.md"""
        results = self.storage.get_by_metadata(
            where={"is_core": 1},
            include=["documents", "metadatas"]
        )
        if not results["documents"]:
            return

        paired = list(zip(results["documents"], results["metadatas"]))
        paired.sort(key=lambda x: x[1].get("created_at", ""))

        text = "# 灰宝的档案\n"
        text += "\n".join(f"- {doc}" for doc, _ in paired)

        Path(self.config.core_file).parent.mkdir(parents=True, exist_ok=True)
        Path(self.config.core_file).write_text(text, encoding="utf-8")
