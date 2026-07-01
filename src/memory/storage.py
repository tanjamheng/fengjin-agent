"""ChromaDB 存储封装"""

from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import MemoryConfig
from ..utils.logger import get_logger


class MemoryStorage:
    """ChromaDB 存储层

    collection 结构：
      - documents: 记忆文本
      - ids: 唯一标识
      - metadatas: {is_core: int, type: str, created_at: str, updated_at: str?}

    通过 chroma_registry 与 RAG DenseIndex 共享同一个 PersistentClient，
    避免重复创建 SQLite 连接和 HNSW 索引元数据。
    """

    def __init__(self, config: MemoryConfig):
        self.config = config
        self.log = get_logger("memory_storage")

        # 通过共享注册表获取 ChromaDB 客户端（与 RAG 共享）
        from ..rag.chroma_registry import acquire as chroma_acquire
        persist_dir = str(Path(config.chroma.persist_directory).resolve())
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        self.client = chroma_acquire(persist_dir)
        self._chroma_shared = True

        # 相对路径解析为项目根目录下的绝对路径
        embedding_model = config.chroma.embedding_model
        if not Path(embedding_model).is_absolute():
            from ..utils.helpers import get_project_root
            embedding_model = str(get_project_root() / embedding_model)

        # 自动检测 GPU 可用性（通过共享注册表，避免与 RAG DenseIndex 重复加载）
        from ..utils.helpers import resolve_device
        from ..rag.embedding_registry import SharedEmbeddingFunction
        effective_device = resolve_device(config.chroma.device)

        self._embedding_fn = SharedEmbeddingFunction(
            model_path=embedding_model,
            device=effective_device,
        )
        self.collection = self._get_or_create_collection(
            config.chroma.collection_name,
            self._embedding_fn,
        )
        # 预热：触发 ChromaDB HNSW 索引加载，避免首次检索 ~500ms 冷启动
        self.collection.count()

    def _get_or_create_collection(self, name: str, ef):
        """创建或获取集合，自动处理 embedding function 冲突（如模型升级导致的签名变化）"""
        hnsw_metadata = {
            "hnsw:space": "cosine",
            "hnsw:M": 8,
            "hnsw:construction_ef": 50,
            "hnsw:search_ef": 20,
        }
        try:
            return self.client.get_or_create_collection(
                name=name,
                embedding_function=ef,
                metadata=hnsw_metadata,
            )
        except ValueError as e:
            if "embedding function conflict" in str(e).lower():
                self.log.warning(
                    "检测到 embedding function 冲突，自动重建集合（旧模型签名不兼容）: {}",
                    str(e).split("conflict:")[-1].strip() if "conflict:" in str(e) else str(e),
                )
                try:
                    self.client.delete_collection(name)
                except Exception:
                    self.log.debug("删除旧集合失败（将尝试重建）")
                return self.client.get_or_create_collection(
                    name=name,
                    embedding_function=ef,
                    metadata=hnsw_metadata,
                )
            else:
                raise

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
        """关闭 ChromaDB 客户端连接并释放 GPU 模型"""
        if self._embedding_fn is not None:
            try:
                self._embedding_fn.cleanup()
                self._embedding_fn = None
            except Exception as e:
                self.log.warning("Embedding模型释放异常: {}", e)
        self.collection = None
        if self.client is not None:
            if getattr(self, "_chroma_shared", False):
                from ..rag.chroma_registry import release as chroma_release
                chroma_release()
            else:
                try:
                    self.client._system.stop()
                except Exception as e:
                    self.log.warning("ChromaDB 客户端关闭异常: {}", e)
            self.client = None
        self.log.info("MemoryStorage 资源已清理")
