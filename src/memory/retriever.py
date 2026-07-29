"""记忆检索"""

import re
import time
from datetime import datetime
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
        core_path = Path(config.core_file)
        if not core_path.is_absolute():
            from ..utils.helpers import get_project_root
            core_path = get_project_root() / core_path
        self._core_path = core_path
        self.log = get_logger("memory_retriever")
        self._core_cache: str | None = None
        self._core_mtime: float = 0.0

    def invalidate_core_cache(self) -> None:
        """外部写入了 core_memory.md 后调用，使缓存失效"""
        self._core_cache = None
        self._core_mtime = 0.0

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

        t1 = time.monotonic()
        db_text = self._search_db(user_input)
        t_db = (time.monotonic() - t1) * 1000
        if db_text:
            sections.append(f"[相关记忆]\n{db_text}")

        t_total = (time.monotonic() - t_start) * 1000
        result = "\n\n".join(sections)
        if result:
            log.info("记忆检索: core={:.0f}ms db={:.0f}ms 总计={:.0f}ms → {} 条记忆",
                     t_core, t_db, t_total, len(sections))
        else:
            log.debug("记忆检索: 无命中 (core={:.0f}ms, db={:.0f}ms)", t_core, t_db)
        return result

    def _load_core(self) -> str:
        """读取 core_memory.md 内容（缓存 + mtime 失效，避免每对话重复 I/O）"""
        if not self._core_path.exists():
            return ""
        mtime = self._core_path.stat().st_mtime
        if self._core_cache is not None and mtime == self._core_mtime:
            return self._core_cache

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
            content = ""
        self._core_cache = content
        self._core_mtime = mtime
        return content

    def _search_db(self, user_input: str) -> str:
        """从 ChromaDB 检索相关记忆"""
        # 空 collection 跳过：避免无意义的嵌入计算 + 向量搜索
        total_memories = self.storage.count()
        if total_memories == 0:
            return ""
        # 极短输入跳过：单字或空输入不太可能匹配到有意义的记忆
        if len(user_input.strip()) < 2:
            return ""

        top_k = self.config.retrieval.top_k
        candidate_count = min(
            total_memories,
            top_k * self.config.retrieval.candidate_multiplier,
        )

        results = self.storage.query(
            text=user_input,
            n_results=candidate_count,
            where={"is_core": 0}
        )

        if not results["documents"] or not results["documents"][0]:
            return ""

        distances = (results.get("distances") or [[]])[0]
        candidates = []
        for index, (doc, meta) in enumerate(zip(
            results["documents"][0], results["metadatas"][0]
        )):
            base_distance = (
                distances[index] if index < len(distances) else index * 1e-9
            )
            adjusted_distance = base_distance + self._temporary_age_penalty(meta)
            candidates.append((adjusted_distance, index, doc, meta))
        candidates.sort(key=lambda item: (item[0], item[1]))

        entries = []
        for _, _, doc, meta in candidates[:top_k]:
            type_label = "情景" if meta.get("type") == "episodic" else "语义"
            scope_labels = {
                "recurring": "周期性",
                "temporary": "阶段性",
                "event": "一次性事件",
            }
            scope_label = scope_labels.get(meta.get("time_scope"))
            if scope_label:
                type_label += f"，{scope_label}"
            source_time = meta.get("source_timestamp") or meta.get("created_at", "")
            source_date = str(source_time).split("T", 1)[0] if source_time else ""
            date_label = f"，记录于 {source_date}" if source_date else ""
            if source_date and re.search(
                r"今天|今日|昨天|昨日|明天|明日|前天|后天", doc
            ):
                date_label += "，正文相对时间以该日期为准"
            entries.append(f"- ({type_label}{date_label}) {doc}")

        return "\n".join(entries)

    def _temporary_age_penalty(self, metadata: dict) -> float:
        """旧临时状态逐日降权，但不删除，明确询问往事时仍可召回。"""
        if metadata.get("time_scope") != "temporary":
            return 0.0
        reference = (
            metadata.get("event_time")
            or metadata.get("source_timestamp")
            or metadata.get("created_at")
        )
        if not reference:
            return 0.0
        try:
            event_date = datetime.fromisoformat(str(reference)).date()
        except ValueError:
            return 0.0
        age_days = max(0, (datetime.now().astimezone().date() - event_date).days)
        retrieval = self.config.retrieval
        return min(
            retrieval.temporary_max_penalty,
            age_days * retrieval.temporary_decay_per_day,
        )
