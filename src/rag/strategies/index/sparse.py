"""稀疏索引策略（BM25）

基于词频统计的倒排索引，适合关键词检索。
"""

import threading
from typing import List
from .base import IndexStrategy


class SparseIndex(IndexStrategy):
    """BM25 稀疏索引

    文档存储：(id, tokens, content, metadata) 四元组。
    BM25 索引惰性构建：add() 设脏标记，search() 首次调用时重建。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        """
        Args:
            k1: BM25 参数，控制词频饱和度
            b: BM25 参数，控制文档长度归一化
        """
        self.k1 = k1
        self.b = b

        self._bm25 = None
        self._bm25_dirty = False
        self._docs: list[tuple] = []  # [(id, tokens, content, metadata), ...]
        self._lock = threading.RLock()

    def initialize(self) -> None:
        """初始化"""
        pass

    def _tokenize(self, text: str) -> List[str]:
        """简单分词"""
        import re
        text = re.sub(r'[^\w\s一-鿿]', '', text)
        tokens = []
        for char in text:
            if '一' <= char <= '鿿':
                tokens.append(char)
            elif char.strip():
                tokens.append(char)
        words = re.findall(r'[a-zA-Z]+', text)
        tokens.extend(words)
        return tokens

    def add(self, chunks: List, ids: List[str] | None = None) -> None:
        """添加文本块（惰性：不立即重建 BM25）"""
        try:
            import rank_bm25  # noqa: F401
        except ImportError:
            raise ImportError("请安装 rank_bm25: pip install rank_bm25")
        if not chunks:
            return
        with self._lock:
            document_ids = (
                [
                    str(len(self._docs) + offset)
                    for offset in range(len(chunks))
                ]
                if ids is None else ids
            )
            if len(document_ids) != len(chunks):
                raise ValueError("SparseIndex.add 的 ids 数量必须与 chunks 一致")
            self._docs.extend(
                (
                    str(document_id),
                    self._tokenize(chunk.content),
                    chunk.content,
                    chunk.metadata,
                )
                for document_id, chunk in zip(document_ids, chunks)
            )
            self._bm25_dirty = True

    def rebuild(self, records: List[dict]) -> None:
        """根据权威知识块记录完整构建 BM25，成功后一次性发布新状态。"""
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            raise ImportError("请安装 rank_bm25: pip install rank_bm25")

        new_docs = [
            (
                str(record["id"]),
                self._tokenize(record["content"]),
                record["content"],
                record.get("metadata") or {},
            )
            for record in records
        ]
        new_bm25 = (
            BM25Okapi([tokens for _, tokens, _, _ in new_docs])
            if new_docs else None
        )
        with self._lock:
            self._docs = new_docs
            self._bm25 = new_bm25
            self._bm25_dirty = False

    def _ensure_bm25(self) -> None:
        """惰性重建 BM25 索引"""
        if not self._bm25_dirty:
            return
        from rank_bm25 import BM25Okapi
        self._bm25 = BM25Okapi(
            [tokens for _, tokens, _, _ in self._docs]
        ) if self._docs else None
        self._bm25_dirty = False

    def search(self, query: str, top_k: int = 5) -> List[dict]:
        """BM25 搜索"""
        with self._lock:
            self._ensure_bm25()
            if self._bm25 is None:
                return []

            query_tokens = self._tokenize(query)
            scores = self._bm25.get_scores(query_tokens)

            ranked_indices = sorted(
                range(len(scores)), key=lambda i: scores[i], reverse=True
            )[:top_k]

            results = []
            for idx in ranked_indices:
                document_id, _, content, metadata = self._docs[idx]
                results.append({
                    "content": content,
                    "metadata": metadata,
                    "score": scores[idx],
                    "id": document_id,
                })
            return results

    def count(self) -> int:
        """返回文档数量"""
        with self._lock:
            return len(self._docs)

    def cleanup(self) -> None:
        """清理资源"""
        with self._lock:
            self._bm25 = None
            self._docs = []
            self._bm25_dirty = False
