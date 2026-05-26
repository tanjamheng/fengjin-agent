"""稀疏索引策略（BM25）

基于词频统计的倒排索引，适合关键词检索。
"""

from typing import List
from .base import IndexStrategy


class SparseIndex(IndexStrategy):
    """BM25 稀疏索引"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        """
        Args:
            k1: BM25 参数，控制词频饱和度
            b: BM25 参数，控制文档长度归一化
        """
        self.k1 = k1
        self.b = b

        self._bm25 = None
        self._corpus = []
        self._metadatas = []

    def initialize(self) -> None:
        """初始化"""
        # BM25 不需要额外初始化
        pass

    def _tokenize(self, text: str) -> List[str]:
        """简单分词"""
        # 中文简单分词：按字符
        # 英文按空格
        import re
        # 移除标点
        text = re.sub(r'[^\w\s\u4e00-\u9fff]', '', text)
        # 中文按字符，英文按空格
        tokens = []
        for char in text:
            if '\u4e00' <= char <= '\u9fff':
                tokens.append(char)
            elif char.strip():
                tokens.append(char)

        # 英文单词
        words = re.findall(r'[a-zA-Z]+', text)
        tokens.extend(words)

        return tokens

    def add(self, chunks: List) -> None:
        """添加文本块"""
        try:
            from rank_bm25 import BM25Okapi

            for chunk in chunks:
                self._corpus.append(self._tokenize(chunk.content))
                self._metadatas.append(chunk.metadata)

            # 重建 BM25 索引
            self._bm25 = BM25Okapi(self._corpus)

        except ImportError:
            raise ImportError("请安装 rank_bm25: pip install rank_bm25")

    def search(self, query: str, top_k: int = 5) -> List[dict]:
        """BM25 搜索"""
        if self._bm25 is None:
            return []

        query_tokens = self._tokenize(query)
        scores = self._bm25.get_scores(query_tokens)

        # 排序
        ranked_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

        results = []
        for idx in ranked_indices:
            results.append({
                "content": " ".join(self._corpus[idx]),  # 重建原始文本需要额外存储
                "metadata": self._metadatas[idx],
                "score": scores[idx],
                "id": str(idx)
            })

        return results

    def count(self) -> int:
        """返回文档数量"""
        return len(self._corpus)

    def cleanup(self) -> None:
        """清理资源"""
        self._bm25 = None
        self._corpus = []
        self._metadatas = []