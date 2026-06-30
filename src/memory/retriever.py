"""记忆检索"""

import time
from pathlib import Path

from ..utils.logger import get_logger
from .config import MemoryConfig
from .storage import MemoryStorage


class MemoryRetriever:
    """记忆检索器

    两层检索：
    1. core_memory.md 全文读取（~1ms）
    2. ChromaDB 语义 top-K（~30ms）
    """

    def __init__(self, config: MemoryConfig, storage: MemoryStorage):
        self.config = config
        self.storage = storage
        self._core_path = Path(config.core_file)
        self.log = get_logger("memory_retriever")

    def retrieve(self, user_input: str, trace_id: str = "") -> str:
        """检索记忆，返回格式化的记忆文本"""
        log = self.log.bind(trace_id=trace_id) if trace_id else self.log
        t_start = time.monotonic()
        sections = []

        t0 = time.monotonic()
        core_text = self._load_core()
        t_core = (time.monotonic() - t0) * 1000
        if core_text:
            sections.append(f"[核心记忆]\n{core_text}")
            log.debug("core_memory 命中 ({:.0f}ms, {} chars)", t_core, len(core_text))
        else:
            log.debug("core_memory 无内容 ({:.0f}ms)", t_core)

        t1 = time.monotonic()
        db_text = self._search_db(user_input)
        t_db = (time.monotonic() - t1) * 1000
        if db_text:
            entry_count = db_text.count("\n- ")
            sections.append(f"[相关记忆]\n{db_text}")
            log.debug("ChromaDB 命中 ({:.0f}ms, {} 条)", t_db, entry_count)
        else:
            log.debug("ChromaDB 无命中 ({:.0f}ms)", t_db)

        t_total = (time.monotonic() - t_start) * 1000
        result = "\n\n".join(sections)
        if result:
            log.info("记忆检索完成: core={:.0f}ms db={:.0f}ms 总计={:.0f}ms → {} 条记忆",
                     t_core, t_db, t_total, len(sections))
        return result

    def _load_core(self) -> str:
        """读取 core_memory.md 内容（不含标题行和占位符）"""
        if not self._core_path.exists():
            return ""
        lines = self._core_path.read_text(encoding="utf-8").strip().splitlines()
        content = "\n".join(
            line for line in lines
            if line.strip() and not line.startswith("#")
        ).strip()
        # 过滤占位符文本：MemoryWriter 尚未写入真实高重要性事实时的初始内容
        _PLACEHOLDER_PREFIXES = (
            "（风堇会在对话中逐渐了解你",
            "(风堇会在对话中逐渐了解你",
        )
        if content.startswith(_PLACEHOLDER_PREFIXES):
            return ""
        return content

    def _search_db(self, user_input: str) -> str:
        """从 ChromaDB 检索相关记忆"""
        top_k = self.config.retrieval.top_k

        results = self.storage.query(
            text=user_input,
            n_results=top_k,
            where={"is_core": 0}
        )

        if not results["documents"] or not results["documents"][0]:
            return ""

        entries = []
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            type_label = "情景" if meta.get("type") == "episodic" else "语义"
            entries.append(f"- ({type_label}) {doc}")

        return "\n".join(entries)
