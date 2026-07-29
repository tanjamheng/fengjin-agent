"""稠密向量索引策略

使用 embedding 模型将文本转化为向量，存储在向量数据库中。
"""

import uuid
from typing import List
from pathlib import Path
from .base import IndexStrategy
from ....utils.logger import get_logger


class DenseIndex(IndexStrategy):
    """稠密向量索引（Chroma/FAISS）"""

    def __init__(
        self,
        embedding_model: str = "models/bge-m3",
        persist_directory: str = "data/chroma",
        collection_name: str = "fengjin_knowledge",
        store_type: str = "chroma",
        device: str = "cpu"
    ):
        self.embedding_model_name = embedding_model
        self.persist_directory = persist_directory
        self.collection_name = collection_name
        self.store_type = store_type
        self.device = device

        self._embedding_model = None
        self._embedding_is_shared = False
        self._cleaned = False
        self._store = None
        self._collection = None
        self.log = get_logger("dense_index")

    def initialize(self) -> None:
        """初始化（幂等：已初始化时跳过，防止非hybrid+hybrid配置下重复加载）"""
        if self._embedding_model is not None:
            return
        self._cleaned = False  # 重置幂等守卫，支持 cleanup→reinit 序列
        # 初始化 Embedding 模型（通过注册表单例共享，避免重复加载）
        try:
            from ....utils.helpers import get_project_root, resolve_device
            from ... import embedding_registry as _reg
            model_path = self.embedding_model_name
            if not Path(model_path).is_absolute():
                model_path = str(get_project_root() / model_path)
            effective_device = resolve_device(self.device, "bge-m3")
            self._embedding_model = _reg.acquire(model_path, effective_device)
            # 判断是否为共享实例（通过模块属性动态读取，避免按值捕获）
            self._embedding_is_shared = (
                _reg._model is not None
                and _reg._model_path is not None
                and str(model_path) == str(_reg._model_path)
            )
        except ImportError:
            raise ImportError("请安装 sentence-transformers: pip install sentence-transformers")

        # 初始化向量库
        if self.store_type == "chroma":
            self._init_chroma()
        elif self.store_type == "faiss":
            self._init_faiss()
        else:
            raise ValueError(f"不支持的向量库类型: {self.store_type}")

    def _init_chroma(self):
        """初始化 ChromaDB（通过共享注册表，避免重复创建客户端连接）"""
        try:
            from ....utils.helpers import get_project_root
            from ...chroma_registry import acquire as chroma_acquire

            persist_dir = Path(self.persist_directory)
            if not persist_dir.is_absolute():
                persist_dir = get_project_root() / persist_dir
            persist_dir.mkdir(parents=True, exist_ok=True)

            self._store = chroma_acquire(str(persist_dir.resolve()))
            self._chroma_shared = True
            self._collection = self._store.get_or_create_collection(
                name=self.collection_name,
                metadata={
                    "hnsw:space": "cosine",
                    "hnsw:M": 8,
                    "hnsw:construction_ef": 50,
                    "hnsw:search_ef": 20,
                },
            )
            # 预热：触发 ChromaDB HNSW 索引加载，避免首次检索 ~500ms 冷启动
            self._collection.count()
        except ImportError:
            raise ImportError("请安装 chromadb: pip install chromadb")

    def _init_faiss(self):
        """初始化 FAISS"""
        try:
            import faiss
            import numpy as np
            self._faiss_index = None
            self._faiss_docs = []
            self._faiss_metadatas = []
        except ImportError:
            raise ImportError("请安装 faiss: pip install faiss-cpu 或 pip install faiss-gpu")

    def _embed(self, texts: List[str]):
        """生成向量（返回 numpy ndarray，避免 list[list[float]] 6-8× 内存膨胀）"""
        import torch
        with torch.inference_mode():
            return self._embedding_model.encode(
                texts, convert_to_numpy=True, batch_size=64,
                normalize_embeddings=True, show_progress_bar=False,
            )

    @staticmethod
    def generate_ids(chunks: List) -> List[str]:
        """为一批知识块生成可同时交给 Dense/Sparse 的公共 ID。"""
        return [
            f"chunk_{chunk.chunk_id}_{uuid.uuid4().hex[:8]}"
            for chunk in chunks
        ]

    def add(self, chunks: List, ids: List[str] | None = None) -> None:
        """添加文本块"""
        if not chunks:
            return

        texts = [chunk.content for chunk in chunks]
        ids = self.generate_ids(chunks) if ids is None else ids
        if len(ids) != len(chunks):
            raise ValueError("DenseIndex.add 的 ids 数量必须与 chunks 一致")
        embeddings = self._embed(texts)
        metadatas = [chunk.metadata for chunk in chunks]

        if self.store_type == "chroma":
            self._collection.add(
                ids=ids,
                embeddings=embeddings,
                documents=texts,
                metadatas=metadatas
            )
        elif self.store_type == "faiss":
            self._add_to_faiss(embeddings, texts, metadatas)

    def get_records(self) -> List[dict]:
        """读取现有知识块，不读取 Embedding，也不修改持久化数据。"""
        if self.store_type != "chroma":
            raise RuntimeError("只有 Chroma DenseIndex 支持持久化记录恢复")
        if self._collection is None:
            raise RuntimeError("DenseIndex 尚未初始化")

        results = self._collection.get(include=["documents", "metadatas"])
        ids = list(results.get("ids") or [])
        documents = list(results.get("documents") or [])
        metadatas = list(results.get("metadatas") or [])
        if not (len(ids) == len(documents) == len(metadatas)):
            raise RuntimeError(
                "Chroma 知识块数据不完整：ID、Document、Metadata 数量不一致"
            )
        return [
            {
                "id": str(record_id),
                "content": document,
                "metadata": metadata or {},
            }
            for record_id, document, metadata
            in zip(ids, documents, metadatas)
        ]

    def _add_to_faiss(self, embeddings, texts, metadatas):
        """添加到 FAISS"""
        import faiss
        import numpy as np

        embeddings_np = np.array(embeddings).astype('float32')
        dimension = embeddings_np.shape[1]

        if self._faiss_index is None:
            self._faiss_index = faiss.IndexFlatL2(dimension)

        self._faiss_index.add(embeddings_np)
        self._faiss_docs.extend(texts)
        self._faiss_metadatas.extend(metadatas)

    def search(self, query: str, top_k: int = 5) -> List[dict]:
        """搜索"""
        query_embedding = self._embed([query])[0]

        if self.store_type == "chroma":
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                include=["documents", "metadatas", "distances"],
            )
            return self._format_chroma_results(results)
        elif self.store_type == "faiss":
            return self._search_faiss(query_embedding, top_k)

        return []

    def _format_chroma_results(self, results: dict) -> List[dict]:
        """格式化 Chroma 结果"""
        formatted = []
        if not results['documents']:
            return formatted

        for i, doc in enumerate(results['documents'][0]):
            formatted.append({
                "content": doc,
                "metadata": results['metadatas'][0][i] if results['metadatas'] else {},
                "distance": results['distances'][0][i] if results['distances'] else 0,
                "id": results['ids'][0][i]
            })
        return formatted

    def _search_faiss(self, query_embedding: List[float], top_k: int) -> List[dict]:
        """FAISS 搜索"""
        import numpy as np

        query_np = np.array([query_embedding]).astype('float32')
        distances, indices = self._faiss_index.search(query_np, top_k)

        results = []
        for i, idx in enumerate(indices[0]):
            if idx < len(self._faiss_docs):
                results.append({
                    "content": self._faiss_docs[idx],
                    "metadata": self._faiss_metadatas[idx] if idx < len(self._faiss_metadatas) else {},
                    "distance": distances[0][i],
                    "id": str(idx)
                })
        return results

    def count(self) -> int:
        """返回文档数量"""
        if self.store_type == "chroma":
            return self._collection.count()
        elif self.store_type == "faiss":
            return len(self._faiss_docs)
        return 0

    def cleanup(self) -> None:
        """清理资源（不删除数据；幂等：重复调用安全）"""
        if getattr(self, "_cleaned", False):
            return
        self._cleaned = True
        if self._embedding_model is not None:
            if self._embedding_is_shared:
                from ...embedding_registry import release
                release()
            else:
                # 独立实例：直接删除
                del self._embedding_model
            self._embedding_model = None
            self._embedding_is_shared = False
        # 释放 ChromaDB 客户端连接（不删除 collection 数据）
        self._collection = None
        if self._store is not None:
            if getattr(self, "_chroma_shared", False):
                from ...chroma_registry import release as chroma_release
                chroma_release()
            else:
                try:
                    self._store._system.stop()
                except Exception as e:
                    self.log.warning("ChromaDB 客户端关闭异常: {}", e)
            self._store = None
