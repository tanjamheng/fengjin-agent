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

        self._queue: queue.Queue = queue.Queue(maxsize=300)
        self._running = True
        self.log = get_logger("memory_writer")
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()
        self._replay_pending()

    def write(self, facts: list[dict]) -> None:
        """将过滤后的事实加入写入队列（非阻塞）"""
        for fact in facts:
            self._queue.put(fact)

    def stop(self) -> None:
        """停止写入线程，超时后持久化剩余任务以防数据丢失"""
        self._running = False
        pending = self._queue.qsize()
        try:
            self._queue.put(None, timeout=5)
        except queue.Full:
            self.log.warning("写入队列已满，无法发送停止信号，直接持久化剩余任务")
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            remaining = self._queue.qsize()
            self.log.warning("写入线程超时未退出，丢弃队列中约 {} 条任务（原队列 {} 条）", remaining, pending)
            self._dump_pending()

    def _writer_loop(self) -> None:
        """后台写入线程，串行处理所有写入任务"""
        while self._running:
            item = self._queue.get()
            if item is None:
                break
            try:
                self._process_fact(item)
            except Exception as e:
                self.log.error("记忆写入失败: {}", e)
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
        """冲突消解。LLM 合并失败时降级为直接插入新事实。"""
        old_result = self.storage.get(ids=[old_id])
        old_content = old_result["documents"][0]
        old_meta = old_result["metadatas"][0]

        # core 保护：low-importance 不能修改 core 记忆
        if old_meta["is_core"] and not is_core:
            self._insert(fact, is_core=False)
            return

        try:
            merged = self._llm_merge(old_content, fact["content"])
        except Exception as e:
            self.log.error("LLM记忆合并失败，降级为直接插入: {}", e)
            self._insert(fact, is_core)
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
        if not results["documents"]:
            return

        paired = list(zip(results["documents"], results["metadatas"]))
        paired.sort(key=lambda x: x[1].get("created_at", ""))

        text = "# 灰宝的档案\n"
        text += "\n".join(f"- {doc}" for doc, _ in paired)

        Path(self.config.core_file).parent.mkdir(parents=True, exist_ok=True)
        Path(self.config.core_file).write_text(text, encoding="utf-8")

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
                    self._queue.put(fact)
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
            facts.append(item)

        if not facts:
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
                pass

        for fact in facts:
            fact["_dumped_at"] = datetime.now().isoformat()
        existing.extend(facts)

        dump_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        self.log.info("已持久化 {} 条未处理记忆到 {}", len(facts), dump_path)
