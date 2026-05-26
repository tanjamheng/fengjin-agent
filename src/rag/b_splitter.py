"""文本切分器入口

从策略仓库获取切分策略，执行切分操作。
"""

from typing import List, Optional
from pydantic import BaseModel
from .strategies.splitter import get_splitter, SplitterStrategy, TextChunk
from ..utils.logger import get_logger, generate_trace_id


class TextSplitter:
    """文本切分器（入口）"""

    def __init__(
        self,
        strategy_type: str = "recursive",
        strategy_params: dict = None
    ):
        """
        Args:
            strategy_type: 策略类型 (fixed/recursive/semantic/markdown)
            strategy_params: 策略参数
        """
        self.strategy_type = strategy_type
        self.strategy_params = strategy_params or {
            "chunk_size": 512,
            "chunk_overlap": 50
        }
        self.log = get_logger(generate_trace_id())

        self._strategy: Optional[SplitterStrategy] = None

    def _get_strategy(self) -> SplitterStrategy:
        """获取策略实例"""
        if self._strategy is None:
            self._strategy = get_splitter(self.strategy_type, self.strategy_params)
        return self._strategy

    def split(self, text: str, metadata: dict = None) -> List[TextChunk]:
        """切分文本"""
        if not text:
            return []

        self.log.info(f"切分文本，策略: {self.strategy_type}")
        strategy = self._get_strategy()
        chunks = strategy.split(text, metadata)

        self.log.info(f"切分完成，共 {len(chunks)} 个块")
        return chunks

    def split_document(self, document) -> List[TextChunk]:
        """切分文档"""
        from .a_loader import Document
        if isinstance(document, Document):
            return self.split(document.content, document.metadata)
        elif isinstance(document, str):
            return self.split(document)
        else:
            raise ValueError(f"不支持的类型: {type(document)}")