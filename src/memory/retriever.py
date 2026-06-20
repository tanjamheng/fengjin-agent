"""记忆检索"""

from pathlib import Path

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

    def retrieve(self, user_input: str, trace_id: str = "") -> str:
        """检索记忆，返回格式化的记忆文本"""
        sections = []

        core_text = self._load_core()
        if core_text:
            sections.append(f"[核心记忆]\n{core_text}")

        db_text = self._search_db(user_input)
        if db_text:
            sections.append(f"[相关记忆]\n{db_text}")

        return "\n\n".join(sections)

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
