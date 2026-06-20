"""分块策略基类

定义所有分块策略的通用接口。
"""

from abc import ABC, abstractmethod
from typing import List
from pydantic import BaseModel


class TextChunk(BaseModel):
    """文本块"""
    content: str
    metadata: dict = {}
    chunk_id: int = 0


class SplitterStrategy(ABC):
    """分块策略抽象基类"""

    @abstractmethod
    def split(self, text: str, metadata: dict = None) -> List[TextChunk]:
        """切分文本

        Args:
            text: 待切分的文本
            metadata: 元数据（会附加到每个 chunk）

        Returns:
            切分后的文本块列表
        """
        pass

    def cleanup(self) -> None:
        """清理资源（子类可按需重写）"""
        pass

    def split_document(self, document) -> List[TextChunk]:
        """切分文档对象"""
        from ...a_loader import Document
        if isinstance(document, Document):
            return self.split(document.content, document.metadata)
        elif isinstance(document, str):
            return self.split(document)
        else:
            raise ValueError(f"不支持的类型: {type(document)}")