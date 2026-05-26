"""固定长度分块策略

按固定字符数切割文本，支持重叠。
"""

from typing import List
from .base import SplitterStrategy, TextChunk


class FixedSplitter(SplitterStrategy):
    """固定长度分块"""

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 50):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, text: str, metadata: dict = None) -> List[TextChunk]:
        """切分文本"""
        if not text:
            return []

        chunks = []
        start = 0
        chunk_id = 0
        base_metadata = metadata or {}

        while start < len(text):
            end = start + self.chunk_size
            chunk_text = text[start:end]

            chunks.append(TextChunk(
                content=chunk_text,
                metadata={**base_metadata, "chunk_index": chunk_id, "strategy": "fixed"},
                chunk_id=chunk_id
            ))

            # 下一块的起始位置（考虑重叠）
            start = end - self.chunk_overlap
            chunk_id += 1

        return chunks