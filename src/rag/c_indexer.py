"""索引构建入口

从策略仓库获取索引策略，执行索引操作。
"""

from typing import List, Optional
from pathlib import Path
from .strategies.index import get_index, IndexStrategy
from .strategies.splitter import TextChunk
from ..utils.logger import get_logger, generate_trace_id


class Indexer:
    """索引器（入口）"""

    def __init__(
        self,
        strategy_type: str = "dense",
        strategy_params: dict = None
    ):
        """
        Args:
            strategy_type: 策略类型 (dense/sparse/hybrid)
            strategy_params: 策略参数
        """
        self.strategy_type = strategy_type
        self.strategy_params = strategy_params or {
            "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            "persist_directory": "data/chroma",
            "collection_name": "default",
            "store_type": "chroma",
            "device": "auto"
        }
        self.log = get_logger("rag_indexer")

        self._strategy: Optional[IndexStrategy] = None

    def _get_strategy(self) -> IndexStrategy:
        """获取策略实例"""
        if self._strategy is None:
            self._strategy = get_index(self.strategy_type, self.strategy_params)
        return self._strategy

    @property
    def strategy(self) -> IndexStrategy:
        """公开访问策略实例（供外部组装，如 Retriever 引用 IndexStrategy）"""
        return self._get_strategy()

    def initialize(self) -> None:
        """初始化索引"""
        self.log.info("初始化索引，策略: {}", self.strategy_type)
        strategy = self._get_strategy()
        strategy.initialize()
        self.log.info("索引初始化完成")

    def add(self, chunks: List[TextChunk]) -> None:
        """添加文本块"""
        if not chunks:
            return

        strategy = self._get_strategy()
        strategy.add(chunks)
        self.log.info("添加 {} 个文本块到索引", len(chunks))

    def search(self, query: str, top_k: int = 5) -> List[dict]:
        """搜索"""
        strategy = self._get_strategy()
        return strategy.search(query, top_k)

    def count(self) -> int:
        """返回文档数量"""
        strategy = self._get_strategy()
        return strategy.count()

    def cleanup(self) -> None:
        """清理资源"""
        if self._strategy:
            self._strategy.cleanup()
        self.log.info("索引资源已清理")