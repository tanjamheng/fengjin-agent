"""RAG 服务层

纯功能类，不依赖任何 Skill/Agent 框架。
封装完整的 RAG 管道：加载 → 切分 → 索引 → 检索 → 重排序。
"""

import threading
from typing import Optional, List
from .a_loader import DocumentLoader
from .b_splitter import TextSplitter
from .c_indexer import Indexer
from .d_retriever import Retriever
from .e_query_enhancer import QueryEnhancer
from .f_reranker import Reranker
from .strategies.retriever.base import SearchResult
from ..config import RAGSettings
from ..utils.logger import get_logger


class RAGService:
    """RAG 检索服务"""

    def __init__(self, config: Optional[RAGSettings] = None, llm_client=None):
        self.config = config or RAGSettings.load()
        self.rag_config = self.config.rag
        self.llm_client = llm_client

        self.loader: Optional[DocumentLoader] = None
        self.splitter: Optional[TextSplitter] = None
        self.indexer: Optional[Indexer] = None
        self.retriever: Optional[Retriever] = None
        self.query_enhancer: Optional[QueryEnhancer] = None
        self.reranker: Optional[Reranker] = None

        self.log = get_logger()
        self._initialized = False
        self._init_lock = threading.Lock()

    def initialize(self) -> None:
        """初始化 RAG 管道组件（线程安全，部分失败时回滚已初始化的组件）"""
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            self.log.info("初始化 RAG 服务...")

            try:
                self.loader = DocumentLoader(
                    supported_formats=self.rag_config.loader.supported_formats,
                    max_file_size_mb=self.rag_config.loader.max_file_size_mb
                )

                self.splitter = TextSplitter(
                    strategy_type=self.rag_config.splitter.type,
                    strategy_params=self.rag_config.splitter.params
                )

                self.indexer = Indexer(
                    strategy_type=self.rag_config.index.type,
                    strategy_params=self.rag_config.index.params
                )
                self.indexer.initialize()

                # 将 llm_client 注入 retriever 参数（HyDE 策略需要）
                retriever_params = dict(self.rag_config.retriever.params)
                retriever_params.setdefault("llm_client", self.llm_client)
                self.retriever = Retriever(
                    index=self.indexer.strategy,
                    strategy_type=self.rag_config.retriever.type,
                    strategy_params=retriever_params
                )
                if not (self.rag_config.index.type == "hybrid"
                        and self.rag_config.retriever.type == "hybrid"):
                    self.retriever.initialize()

                self.query_enhancer = QueryEnhancer(
                    strategy_type=self.rag_config.query_enhancer.type,
                    strategy_params=self.rag_config.query_enhancer.params,
                    llm_client=self.llm_client
                )

                self.reranker = Reranker(
                    strategy_type=self.rag_config.reranker.type,
                    strategy_params=self.rag_config.reranker.params,
                    llm_client=self.llm_client
                )
                self.reranker.initialize()

                self._initialized = True
                self.log.info("RAG 服务初始化完成")
            except Exception:
                # 部分初始化失败：清理已初始化的子组件，避免资源泄漏
                self.log.opt(exception=True).error("RAG 初始化失败，回滚已初始化的子组件")
                if self.reranker:
                    self.reranker.cleanup()
                    self.reranker = None
                if self.query_enhancer:
                    self.query_enhancer.cleanup()
                    self.query_enhancer = None
                if self.retriever:
                    self.retriever.cleanup()
                    self.retriever = None
                if self.indexer:
                    self.indexer.cleanup()
                    self.indexer = None
                if self.splitter:
                    self.splitter = None
                if self.loader:
                    self.loader = None
                raise

    def retrieve(self, query: str, trace_id: str = "") -> str:
        """执行完整的 RAG 检索管道，返回上下文文本

        Args:
            query: 检索查询
            trace_id: 请求追踪ID

        Returns:
            检索到的上下文文本（无结果时返回空字符串）
        """
        if not self._initialized:
            self.initialize()

        log = self.log.bind(trace_id=trace_id) if trace_id else self.log
        log.info("RAG 检索: {}...", query[:50])

        # 查询增强
        enhanced_query = self.query_enhancer.enhance(query)

        # 召回
        if isinstance(enhanced_query, list):
            all_results = []
            for q in enhanced_query:
                results = self.retriever.retrieve(q)
                all_results.extend(results)
            recall_results = self._deduplicate_results(all_results)
        else:
            recall_results = self.retriever.retrieve(enhanced_query)

        # 精排
        reranked_results = self.reranker.rerank(query, recall_results)

        # 构建上下文
        context_text = self._build_context(reranked_results, max_length=1500)
        log.info("RAG 检索完成: 召回 {} 条, 精排 {} 条", len(recall_results), len(reranked_results))

        return context_text

    def ingest_document(self, file_path: str, category: str = "") -> dict:
        """导入单个文档到知识库

        Returns:
            包含 file_path, chunk_count 等信息的字典
        """
        if not self._initialized:
            self.initialize()

        self.log.info("导入文档: {}", file_path)

        document = self.loader.load(file_path, category=category)
        chunks = self.splitter.split_document(document)
        self.indexer.add(chunks)

        return {
            "file_path": file_path,
            "chunk_count": len(chunks),
            "document_name": document.metadata.get("file_name", ""),
            "category": category,
        }

    def ingest_directory(self, dir_path: str, recursive: bool = True) -> dict:
        """导入目录下所有文档

        Returns:
            包含 document_count, total_chunks 等信息的字典
        """
        if not self._initialized:
            self.initialize()

        self.log.info("批量导入: {}", dir_path)

        if recursive:
            documents = self.loader.load_directory_recursive(dir_path)
        else:
            documents = self.loader.load_directory(dir_path)

        total_chunks = 0
        for doc in documents:
            chunks = self.splitter.split_document(doc)
            self.indexer.add(chunks)
            total_chunks += len(chunks)

        return {
            "dir_path": dir_path,
            "document_count": len(documents),
            "total_chunks": total_chunks,
        }

    def get_stats(self) -> dict:
        """获取知识库统计信息"""
        return {
            "initialized": self._initialized,
            "document_count": self.indexer.count() if self.indexer else 0,
            "splitter_type": self.rag_config.splitter.type,
            "index_type": self.rag_config.index.type,
            "retriever_type": self.rag_config.retriever.type,
            "reranker_type": self.rag_config.reranker.type,
            "query_enhancer_type": self.rag_config.query_enhancer.type,
        }

    def cleanup(self) -> None:
        """清理资源（不删除持久化数据）"""
        if self.reranker:
            self.reranker.cleanup()
            self.reranker = None
        if self.retriever:
            self.retriever.cleanup()
            self.retriever = None
        if self.indexer:
            self.indexer.cleanup()
            self.indexer = None
        if self.query_enhancer:
            self.query_enhancer.cleanup()
            self.query_enhancer = None
        if self.loader:
            self.loader = None
        if self.splitter:
            self.splitter.cleanup()
            self.splitter = None

        self._initialized = False
        self.log.info("RAG 服务资源已清理")

    def _deduplicate_results(self, results: List[SearchResult]) -> List[SearchResult]:
        """去重检索结果"""
        seen = set()
        unique = []
        for result in results:
            if result.content not in seen:
                seen.add(result.content)
                unique.append(result)
        return unique

    def _build_context(self, results: List[SearchResult], max_length: int) -> str:
        """构建上下文（含前缀和分隔符，总长度不超过 max_length）"""
        context_parts = []
        current_length = 0
        separator = "\n\n---\n\n"

        for result in results:
            category = result.metadata.get("category", "")
            source_label = f"{category}/{result.source}" if category else result.source
            header = f"[来源: {source_label}]\n"
            entry_len = len(header) + len(result.content)
            # 计入分隔符（非首条结果需要分隔符）
            if context_parts:
                entry_len += len(separator)
            if current_length + entry_len > max_length:
                break
            context_parts.append(header + result.content)
            current_length += entry_len

        return separator.join(context_parts)
