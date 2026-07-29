"""BM25 跨进程恢复与 Hybrid 公共 ID 回归测试。"""

import unittest
from types import SimpleNamespace

from src.rag.strategies.index.dense import DenseIndex
from src.rag.strategies.index.hybrid import HybridIndex
from src.rag.strategies.index.sparse import SparseIndex


class _FakeCollection:
    def __init__(self, records):
        self.records = records
        self.includes = None

    def get(self, include):
        self.includes = include
        return {
            "ids": [record["id"] for record in self.records],
            "documents": [record["content"] for record in self.records],
            "metadatas": [record["metadata"] for record in self.records],
        }


class _FakeDense:
    store_type = "chroma"

    def __init__(self, records=None, *, recovery_error=None):
        self.records = list(records or [])
        self.recovery_error = recovery_error
        self.added_ids = None

    def initialize(self):
        pass

    def get_records(self):
        if self.recovery_error:
            raise self.recovery_error
        return list(self.records)

    def count(self):
        return len(self.records)

    def generate_ids(self, chunks):
        return [f"shared-{index}" for index, _ in enumerate(chunks)]

    def add(self, chunks, ids=None):
        self.added_ids = list(ids or [])
        self.records.extend(
            {
                "id": document_id,
                "content": chunk.content,
                "metadata": chunk.metadata,
            }
            for document_id, chunk in zip(self.added_ids, chunks)
        )

    def search(self, _query, top_k=5):
        return [
            {
                "id": record["id"],
                "content": record["content"],
                "metadata": record["metadata"],
                "distance": 0.1,
            }
            for record in self.records[:top_k]
        ]

    def cleanup(self):
        pass


def _hybrid_with_dense(dense):
    hybrid = HybridIndex()
    hybrid.dense_index = dense
    hybrid.sparse_index = SparseIndex()
    return hybrid


class BM25RecoveryTests(unittest.TestCase):
    def test_dense_reads_documents_without_embedding(self):
        records = [{
            "id": "chunk-existing",
            "content": "神悟树庭",
            "metadata": {"file_name": "剧情.md"},
        }]
        dense = object.__new__(DenseIndex)
        dense.store_type = "chroma"
        dense._collection = _FakeCollection(records)
        dense._embed = lambda _texts: self.fail("恢复 BM25 不应调用 Embedding")

        restored = dense.get_records()

        self.assertEqual(restored, records)
        self.assertEqual(
            dense._collection.includes,
            ["documents", "metadatas"],
        )

    def test_restart_restores_sparse_with_chroma_ids(self):
        records = [{
            "id": "chunk-existing",
            "content": "风堇居住在神悟树庭",
            "metadata": {"file_name": "角色设定.md"},
        }]
        hybrid = _hybrid_with_dense(_FakeDense(records))

        hybrid.initialize()
        sparse_results = hybrid.sparse_index.search("神悟树庭", top_k=1)

        self.assertTrue(hybrid._sparse_ready)
        self.assertEqual(hybrid.dense_index.count(), 1)
        self.assertEqual(hybrid.sparse_index.count(), 1)
        self.assertEqual(sparse_results[0]["id"], "chunk-existing")

    def test_runtime_add_uses_same_ids_for_dense_and_sparse(self):
        hybrid = _hybrid_with_dense(_FakeDense())
        hybrid.initialize()
        chunks = [
            SimpleNamespace(
                chunk_id=0,
                content="翁法罗斯的神悟树庭",
                metadata={"file_name": "世界观.md"},
            )
        ]

        hybrid.add(chunks)
        sparse_results = hybrid.sparse_index.search("神悟树庭", top_k=1)

        self.assertEqual(hybrid.dense_index.added_ids, ["shared-0"])
        self.assertEqual(sparse_results[0]["id"], "shared-0")
        self.assertEqual(hybrid.dense_index.count(), hybrid.sparse_index.count())

    def test_rrf_accumulates_dense_and_sparse_for_shared_id(self):
        hybrid = HybridIndex(dense_weight=0.7, sparse_weight=0.3)
        dense = [{
            "id": "same-chunk",
            "content": "共同知识块",
            "metadata": {},
        }]
        sparse = [{
            "id": "same-chunk",
            "content": "共同知识块",
            "metadata": {},
        }]

        results = hybrid._rrf_fusion(dense, sparse, top_k=5)

        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0]["rrf_score"], 1 / 61)

    def test_sparse_recovery_failure_degrades_to_dense_only(self):
        dense = _FakeDense(
            [{
                "id": "dense-only",
                "content": "仍可使用的稠密知识",
                "metadata": {},
            }],
            recovery_error=RuntimeError("broken collection"),
        )
        hybrid = _hybrid_with_dense(dense)

        hybrid.initialize()
        results = hybrid.search("知识", top_k=1)

        self.assertFalse(hybrid._sparse_ready)
        self.assertEqual([item["id"] for item in results], ["dense-only"])


if __name__ == "__main__":
    unittest.main()
