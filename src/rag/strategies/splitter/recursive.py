"""递归分块策略

按分隔符层级递归切割（段落→句子→词），优先保留自然分隔符。
"""

from typing import List
from .base import SplitterStrategy, TextChunk


class RecursiveSplitter(SplitterStrategy):
    """递归分块（LangChain 风格）"""

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        separators: List[str] = None
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        # 默认分隔符优先级：段落 → 中文句子 → 英文句子 → 词
        self.separators = separators or ["\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";", " ", ""]

    def split(self, text: str, metadata: dict = None) -> List[TextChunk]:
        """切分文本"""
        if not text:
            return []

        # 尝试使用 LangChain 的 RecursiveCharacterTextSplitter
        try:
            from langchain.text_splitter import RecursiveCharacterTextSplitter

            splitter = RecursiveCharacterTextSplitter(
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
                separators=self.separators
            )

            raw_chunks = splitter.split_text(text)
        except ImportError:
            # LangChain 未安装，使用简化版递归切分
            raw_chunks = self._recursive_split_simple(text)

        # 构建 TextChunk 对象
        chunks = []
        base_metadata = metadata or {}
        for i, chunk_text in enumerate(raw_chunks):
            chunks.append(TextChunk(
                content=chunk_text,
                metadata={**base_metadata, "chunk_index": i, "strategy": "recursive"},
                chunk_id=i
            ))

        return chunks

    def _recursive_split_simple(self, text: str) -> List[str]:
        """简化版递归切分（不依赖 LangChain）"""
        if len(text) <= self.chunk_size:
            return [text]

        # 找最佳分隔点
        best_split = -1
        for sep in self.separators:
            if sep == "":
                continue
            # 在 chunk_size 范围内找最后一个分隔符
            search_region = text[:self.chunk_size + 100]
            last_sep = search_region.rfind(sep)
            if last_sep > 0 and last_sep < self.chunk_size:
                best_split = last_sep + len(sep)
                break

        if best_split == -1:
            # 没找到分隔符，强制在 chunk_size 处切割
            best_split = self.chunk_size

        first_chunk = text[:best_split]
        remaining = text[best_split:]

        # 递归处理剩余部分
        other_chunks = self._recursive_split_simple(remaining)

        return [first_chunk] + other_chunks