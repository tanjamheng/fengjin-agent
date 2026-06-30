"""混合索引策略

结合稠密向量索引和稀疏索引（BM25），检索时融合结果。
"""

from typing import List
from .base import IndexStrategy
from .dense import DenseIndex
from .sparse import SparseIndex


class HybridIndex(IndexStrategy):
    """混合索引（Dense + Sparse）"""

    def __init__(
        self,
        embedding_model: str = "models/bge-m3",
        persist_directory: str = "data/chroma",
        collection_name: str = "fengjin_knowledge",
        store_type: str = "chroma",
        device: str = "cpu",
        dense_weight: float = 0.7,
        sparse_weight: float = 0.3
    ):
        self.dense_weight = dense_weight
        self.sparse_weight = sparse_weight

        # 创建子索引
        self.dense_index = DenseIndex(
            embedding_model=embedding_model,
            persist_directory=persist_directory,
            collection_name=f"{collection_name}_dense",
            store_type=store_type,
            device=device
        )
        self.sparse_index = SparseIndex()

    def initialize(self) -> None:
        """初始化两个子索引"""
        self.dense_index.initialize()
        self.sparse_index.initialize()

    def add(self, chunks: List) -> None:
        """添加到两个索引"""
        self.dense_index.add(chunks)
        self.sparse_index.add(chunks)

    def search(self, query: str, top_k: int = 5) -> List[dict]:
        """混合搜索 + RRF 融合"""
        # 从两个索引分别搜索
        dense_results = self.dense_index.search(query, top_k=top_k * 2)
        sparse_results = self.sparse_index.search(query, top_k=top_k * 2)

        # RRF 融合
        return self._rrf_fusion(dense_results, sparse_results, top_k)

    def _rrf_fusion(self, dense_results: List[dict], sparse_results: List[dict], top_k: int) -> List[dict]:
        """Reciprocal Rank Fusion

        公式: score = sum(1 / (k + rank)) for each result list
        """
        k = 60  # RRF 参数

        # 收集所有文档
        all_docs = {}

        # Dense 结果
        for rank, result in enumerate(dense_results):
            doc_id = result.get("id", str(hash(result["content"])))
            all_docs[doc_id] = result
            rrf_score = 1 / (k + rank + 1)
            if "rrf_score" not in all_docs[doc_id]:
                all_docs[doc_id]["rrf_score"] = 0
            all_docs[doc_id]["rrf_score"] += rrf_score * self.dense_weight

        # Sparse 结果
        for rank, result in enumerate(sparse_results):
            doc_id = result.get("id", str(hash(result["content"])))
            if doc_id not in all_docs:
                all_docs[doc_id] = result
                all_docs[doc_id]["rrf_score"] = 0
            rrf_score = 1 / (k + rank + 1)
            all_docs[doc_id]["rrf_score"] += rrf_score * self.sparse_weight

        # 按 RRF 分数排序
        sorted_docs = sorted(all_docs.values(), key=lambda x: x["rrf_score"], reverse=True)[:top_k]

        return sorted_docs

    def count(self) -> int:
        """返回文档数量"""
        return self.dense_index.count()

    def cleanup(self) -> None:
        """清理两个索引"""
        self.dense_index.cleanup()
        self.sparse_index.cleanup()