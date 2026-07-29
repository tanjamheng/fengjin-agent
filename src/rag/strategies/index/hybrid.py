"""混合索引策略

结合稠密向量索引和稀疏索引（BM25），检索时融合结果。
"""

from typing import List
from .base import IndexStrategy
from .dense import DenseIndex
from .sparse import SparseIndex
from ....utils.logger import get_logger


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
        self.log = get_logger("hybrid_index")
        self._sparse_ready = False

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
        self._restore_sparse_from_dense()

    def add(self, chunks: List) -> None:
        """添加到两个索引"""
        if not chunks:
            return
        if self.dense_index.store_type == "chroma":
            common_ids = self.dense_index.generate_ids(chunks)
            sparse_was_ready = self._sparse_ready
            self.dense_index.add(chunks, ids=common_ids)
            # Dense 已提交而 Sparse 尚未同步的短窗口内只允许 Dense 检索。
            self._sparse_ready = False
            if sparse_was_ready:
                try:
                    self.sparse_index.add(chunks, ids=common_ids)
                    if self.sparse_index.count() == self.dense_index.count():
                        self._sparse_ready = True
                        return
                    self.log.error(
                        "BM25 增量更新后数量不一致，将从 Chroma 整体恢复: "
                        "dense={} sparse={}",
                        self.dense_index.count(),
                        self.sparse_index.count(),
                    )
                except Exception as exc:
                    self.log.error(
                        "BM25 增量更新失败，将从 Chroma 整体恢复: {}", exc
                    )
            self._restore_sparse_from_dense()
            return

        # 非 Chroma 路径保持原有进程内行为。
        self.dense_index.add(chunks)
        self.sparse_index.add(chunks)
        self._sparse_ready = True

    def search(self, query: str, top_k: int = 5) -> List[dict]:
        """混合搜索 + RRF 融合"""
        # 从两个索引分别搜索
        dense_results = self.dense_index.search(query, top_k=top_k * 2)
        sparse_results = (
            self.sparse_index.search(query, top_k=top_k * 2)
            if self._sparse_ready else []
        )

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

    def _restore_sparse_from_dense(self) -> None:
        """从 Chroma 权威知识块恢复 BM25；失败时明确降级为 Dense-only。"""
        if self.dense_index.store_type != "chroma":
            self._sparse_ready = True
            return
        try:
            records = self.dense_index.get_records()
            self.sparse_index.rebuild(records)
            dense_count = self.dense_index.count()
            sparse_count = self.sparse_index.count()
            if dense_count != sparse_count:
                raise RuntimeError(
                    f"Dense/Sparse 数量不一致: {dense_count}/{sparse_count}"
                )
            self._sparse_ready = True
            self.log.info(
                "BM25 已从 Chroma 恢复: dense={} sparse={}",
                dense_count,
                sparse_count,
            )
        except Exception as exc:
            self._sparse_ready = False
            self.sparse_index.cleanup()
            self.log.error("BM25 恢复失败，当前降级为 Dense-only: {}", exc)

    def count(self) -> int:
        """返回文档数量"""
        return self.dense_index.count()

    def cleanup(self) -> None:
        """清理两个索引"""
        self.dense_index.cleanup()
        self.sparse_index.cleanup()
        self._sparse_ready = False
