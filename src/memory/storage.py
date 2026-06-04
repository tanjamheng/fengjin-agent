"""ChromaDB 存储封装"""

from datetime import datetime
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from .config import MemoryConfig


class MemoryStorage:
    """ChromaDB 存储层

    collection 结构：
      - documents: 记忆文本
      - ids: 唯一标识
      - metadatas: {is_core: int, type: str, created_at: str, updated_at: str?}
    """

    def __init__(self, config: MemoryConfig):
        self.config = config
        self.client = chromadb.PersistentClient(path=config.chroma.persist_directory)

        # 相对路径解析为项目根目录下的绝对路径
        embedding_model = config.chroma.embedding_model
        if not Path(embedding_model).is_absolute():
            embedding_model = str(Path(__file__).parent.parent.parent / embedding_model)

        embedding_fn = SentenceTransformerEmbeddingFunction(
            model_name=embedding_model
        )
        self.collection = self.client.get_or_create_collection(
            name=config.chroma.collection_name,
            embedding_function=embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )

    def add(self, memory_id: str, content: str, is_core: bool,
            memory_type: str) -> None:
        """添加一条记忆"""
        self.collection.add(
            documents=[content],
            ids=[memory_id],
            metadatas=[{
                "is_core": int(is_core),
                "type": memory_type,
                "created_at": datetime.now().isoformat()
            }]
        )

    def query(self, text: str, n_results: int = 1,
              where: Optional[dict] = None) -> dict:
        """向量相似度查询"""
        return self.collection.query(
            query_texts=[text],
            n_results=n_results,
            where=where,
            include=["documents", "metadatas", "distances"]
        )

    def get(self, ids: list[str],
            include: Optional[list] = None) -> dict:
        """按 ID 获取记忆"""
        if include is None:
            include = ["documents", "metadatas"]
        return self.collection.get(ids=ids, include=include)

    def get_by_metadata(self, where: dict,
                        include: Optional[list] = None) -> dict:
        """按 metadata 过滤获取，空 where 返回全部"""
        if include is None:
            include = ["documents", "metadatas"]
        if not where:
            return self.collection.get(include=include)
        return self.collection.get(where=where, include=include)

    def upsert(self, memory_id: str, content: str, metadata: dict) -> None:
        """更新或插入"""
        self.collection.upsert(
            documents=[content],
            ids=[memory_id],
            metadatas=[metadata]
        )

    def delete(self, ids: Optional[list] = None,
               where: Optional[dict] = None) -> None:
        """删除记忆"""
        if ids is None and (where is None or where == {}):
            all_ids = self.collection.get(include=[])["ids"]
            if all_ids:
                self.collection.delete(ids=all_ids)
        else:
            self.collection.delete(ids=ids, where=where)

    def count(self) -> int:
        return self.collection.count()

    def cleanup(self) -> None:
        """关闭 ChromaDB 客户端连接"""
        self.collection = None
        self.client = None
