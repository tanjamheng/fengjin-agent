"""重排序入口

从策略仓库获取重排序策略，执行重排序操作。
"""

from typing import List, Optional
from .strategies.retriever.base import SearchResult
from .strategies.reranker import get_reranker, RerankerStrategy
from ..utils.logger import get_logger, generate_trace_id


class Reranker:
    """重排序器（入口）"""

    def __init__(
        self,
        strategy_type: str = "none",
        strategy_params: dict = None,
        llm_client=None
    ):
        """
        Args:
            strategy_type: 策略类型 (none/cross_encoder/llm)
            strategy_params: 策略参数
            llm_client: LLM 客户端（用于 llm 策略）
        """
        self.strategy_type = strategy_type
        self.strategy_params = strategy_params or {"top_n": 3}
        self.llm_client = llm_client
        self.log = get_logger("rag_reranker")

        self._strategy: Optional[RerankerStrategy] = None

    def _get_strategy(self) -> RerankerStrategy:
        """获取策略实例"""
        if self._strategy is None:
            params = {**self.strategy_params}
            if self.strategy_type == "llm":
                params["llm_client"] = self.llm_client
            self._strategy = get_reranker(self.strategy_type, params)
        return self._strategy

    def initialize(self) -> None:
        """初始化"""
        self.log.info("初始化重排序器，策略: {}", self.strategy_type)
        strategy = self._get_strategy()
        strategy.initialize()
        self.log.info("重排序器初始化完成")

    def rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        """重排序"""
        if self.strategy_type == "none":
            return results

        self.log.info("重排序 {} 个结果", len(results))
        strategy = self._get_strategy()
        reranked = strategy.rerank(query, results)
        self.log.info("重排序完成，返回 {} 个结果", len(reranked))
        return reranked

    def cleanup(self) -> None:
        """清理资源"""
        if self._strategy:
            self._strategy.cleanup()