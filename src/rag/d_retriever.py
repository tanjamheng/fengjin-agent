"""检索器入口

从策略仓库获取检索策略，执行检索操作。
"""

from typing import List, Optional
from .strategies.retriever import get_retriever, RetrieverStrategy, SearchResult
from .strategies.index import IndexStrategy
from ..utils.logger import get_logger, generate_trace_id


class Retriever:
    """检索器（入口）"""

    def __init__(
        self,
        index: IndexStrategy,
        strategy_type: str = "top_k",
        strategy_params: dict = None
    ):
        """
        Args:
            index: 索引策略实例
            strategy_type: 策略类型 (top_k/hybrid/parent_doc/hyde)
            strategy_params: 策略参数
        """
        self.index = index
        self.strategy_type = strategy_type
        self.strategy_params = strategy_params or {
            "top_k": 5,
            "score_threshold": 0.7
        }
        self.log = get_logger(generate_trace_id())

        self._strategy: Optional[RetrieverStrategy] = None

    def _get_strategy(self) -> RetrieverStrategy:
        """获取策略实例"""
        if self._strategy is None:
            self._strategy = get_retriever(self.strategy_type, self.strategy_params, self.index)
        return self._strategy

    def initialize(self) -> None:
        """初始化"""
        self.log.info(f"初始化检索器，策略: {self.strategy_type}")
        strategy = self._get_strategy()
        strategy.initialize()
        self.log.info("检索器初始化完成")

    def retrieve(self, query: str) -> List[SearchResult]:
        """检索"""
        self.log.info(f"检索查询: {query[:50]}...")
        strategy = self._get_strategy()
        results = strategy.retrieve(query)
        self.log.info(f"检索到 {len(results)} 个相关文档")
        return results

    def get_context(self, query: str, max_length: int = 2000) -> str:
        """获取检索上下文"""
        results = self.retrieve(query)

        context_parts = []
        current_length = 0

        for result in results:
            if current_length + len(result.content) > max_length:
                break
            context_parts.append(f"[来源: {result.source}]\n{result.content}")
            current_length += len(result.content)

        return "\n\n---\n\n".join(context_parts)

    def cleanup(self) -> None:
        """清理资源"""
        if self._strategy:
            self._strategy.cleanup()
        self.log.info("检索器资源已清理")