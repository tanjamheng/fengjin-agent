"""父文档检索策略

小块检索，返回对应的大块父文档。

核心思想：
- 索引时：将大文档切分为小块（child），同时保留大块（parent）映射
- 检索时：用小块检索，返回对应的大块父文档
"""

from typing import List, Dict
from .base import RetrieverStrategy, SearchResult


class ParentDocRetriever(RetrieverStrategy):
    """父文档检索"""

    def __init__(
        self,
        index,  # DenseIndex 实例
        top_k: int = 5,
        child_chunk_size: int = 256,
        parent_chunk_size: int = 1024,
        score_threshold: float = 0.7
    ):
        """
        Args:
            index: 索引实例
            top_k: 返回父文档数量
            child_chunk_size: 小块大小
            parent_chunk_size: 大块大小
            score_threshold: 分数阈值
        """
        self.index = index
        self.top_k = top_k
        self.child_chunk_size = child_chunk_size
        self.parent_chunk_size = parent_chunk_size
        self.score_threshold = score_threshold

        # 存储 child -> parent 映射
        self._child_to_parent: Dict[str, str] = {}
        self._parent_chunks: Dict[str, str] = {}

    def initialize(self) -> None:
        """初始化索引"""
        self.index.initialize()

    def add_documents(self, chunks: List) -> None:
        """添加文档，构建父子映射

        Args:
            chunks: 原始文本块（大块）
        """
        from ..splitter.base import TextChunk

        # 为每个大块生成小块
        child_chunks = []
        for parent_chunk in chunks:
            parent_id = f"parent_{parent_chunk.chunk_id}"

            # 切分大块为小块
            text = parent_chunk.content
            for i in range(0, len(text), self.child_chunk_size):
                child_text = text[i:i + self.child_chunk_size]
                child_id = f"{parent_id}_child_{i}"

                child_chunks.append(TextChunk(
                    content=child_text,
                    metadata={
                        **parent_chunk.metadata,
                        "parent_id": parent_id,
                        "child_index": i
                    },
                    chunk_id=len(child_chunks)
                ))

                # 记录映射
                self._child_to_parent[child_id] = parent_id

            # 存储大块
            self._parent_chunks[parent_id] = text

        # 添加小块到索引
        self.index.add(child_chunks)

    def retrieve(self, query: str) -> List[SearchResult]:
        """检索小块，返回父文档"""
        raw_results = self.index.search(query, top_k=self.top_k * 2)

        # 获取父文档 ID
        parent_ids = set()
        parent_scores = {}

        for item in raw_results:
            score = self._convert_score(item.get("distance", 0))
            parent_id = item.get("metadata", {}).get("parent_id", "")

            if parent_id and score >= self.score_threshold:
                parent_ids.add(parent_id)
                # 记录最高分数
                if parent_id not in parent_scores or score > parent_scores[parent_id]:
                    parent_scores[parent_id] = score

        # 返回父文档
        results = []
        for parent_id in list(parent_ids)[:self.top_k]:
            if parent_id in self._parent_chunks:
                results.append(SearchResult(
                    content=self._parent_chunks[parent_id],
                    score=parent_scores[parent_id],
                    metadata={"parent_id": parent_id},
                    source=""
                ))

        return results

    def _convert_score(self, distance: float) -> float:
        """转换分数"""
        return 1.0 / (1.0 + distance)

    def cleanup(self) -> None:
        """清理"""
        self.index.cleanup()
        self._child_to_parent.clear()
        self._parent_chunks.clear()