"""策略仓库

提供 RAG 各环节的策略实现，支持配置驱动切换。
"""

from .splitter import get_splitter
from .index import get_index
from .retriever import get_retriever
from .reranker import get_reranker
from .query import get_query_enhancer

__all__ = [
    "get_splitter",
    "get_index",
    "get_retriever",
    "get_reranker",
    "get_query_enhancer"
]