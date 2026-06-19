"""稠密向量索引策略

使用 embedding 模型将文本转化为向量，存储在向量数据库中。
"""

from typing import List
from pathlib import Path
from .base import IndexStrategy
from ....utils.logger import get_logger


class DenseIndex(IndexStrategy):
    """稠密向量索引（Chroma/FAISS）"""

    def __init__(
        self,
        embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        persist_directory: str = "data/chroma",
        collection_name: str = "default",
        store_type: str = "chroma",
        device: str = "cpu"
    ):
        self.embedding_model_name = embedding_model
        self.persist_directory = persist_directory
        self.collection_name = collection_name
        self.store_type = store_type
        self.device = device

        self._embedding_model = None
        self._store = None
        self._collection = None
        self.log = get_logger("dense_index")

    def initialize(self) -> None:
        """初始化"""
        # 初始化 Embedding 模型
        try:
            import torch
            from sentence_transformers import SentenceTransformer
            from ....utils.helpers import get_project_root
            model_path = self.embedding_model_name
            # 相对路径解析为项目根目录下的绝对路径
            if not Path(model_path).is_absolute():
                model_path = str(get_project_root() / model_path)

            # 自动检测 GPU 可用性
            effective_device = self.device
            if self.device in ("cuda", "auto") and not torch.cuda.is_available():
                effective_device = "cpu"
            elif self.device == "auto" and torch.cuda.is_available():
                effective_device = "cuda"

            self._embedding_model = SentenceTransformer(
                model_path,
                device=effective_device
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
        """初始化 ChromaDB"""
        try:
            import chromadb
            from chromadb.config import Settings

            Path(self.persist_directory).mkdir(parents=True, exist_ok=True)

            self._store = chromadb.PersistentClient(
                path=self.persist_directory,
                settings=Settings(anonymized_telemetry=False)
            )
            self._collection = self._store.get_or_create_collection(
                name=self.collection_name
            )
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

    def _embed(self, texts: List[str]) -> List[List[float]]:
        """生成向量"""
        embeddings = self._embedding_model.encode(texts, convert_to_numpy=True)
        return embeddings.tolist()

    def add(self, chunks: List) -> None:
        """添加文本块"""
        if not chunks:
            return

        texts = [chunk.content for chunk in chunks]
        embeddings = self._embed(texts)
        ids = [f"chunk_{chunk.chunk_id}_{hash(chunk.content) % 10000}" for chunk in chunks]
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
                n_results=top_k
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
        """清理资源（不删除数据）"""
        if self._embedding_model is not None:
            del self._embedding_model
            self._embedding_model = None
        # 释放 ChromaDB 客户端连接（不删除 collection 数据）
        self._collection = None
        self._store = None
        # 释放 GPU 缓存
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception as e:
            self.log.warning("CUDA缓存清理异常: {}", e)