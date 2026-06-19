"""查询增强入口

从策略仓库获取查询增强策略，执行查询增强操作。
"""

from typing import Union, List, Optional
from .strategies.query import get_query_enhancer, QueryEnhancerStrategy
from ..utils.logger import get_logger, generate_trace_id


class QueryEnhancer:
    """查询增强器（入口）"""

    def __init__(
        self,
        strategy_type: str = "none",
        strategy_params: dict = None,
        llm_client=None
    ):
        """
        Args:
            strategy_type: 策略类型 (none/rewrite/decompose/expand)
            strategy_params: 策略参数
            llm_client: LLM 客户端（用于需要 LLM 的策略）
        """
        self.strategy_type = strategy_type
        self.strategy_params = strategy_params or {}
        self.llm_client = llm_client
        self.log = get_logger(generate_trace_id())

        self._strategy: Optional[QueryEnhancerStrategy] = None

    def _get_strategy(self) -> QueryEnhancerStrategy:
        """获取策略实例"""
        if self._strategy is None:
            # 为需要 LLM 的策略注入 client
            params = {**self.strategy_params}
            if self.strategy_type in ["rewrite", "decompose", "expand"]:
                params["llm_client"] = self.llm_client
            self._strategy = get_query_enhancer(self.strategy_type, params)
        return self._strategy

    def initialize(self) -> None:
        """初始化"""
        self._get_strategy().initialize()

    def enhance(self, query: str) -> Union[str, List[str]]:
        """增强查询"""
        self.log.info("增强查询，策略: {}", self.strategy_type)
        strategy = self._get_strategy()
        enhanced = strategy.enhance(query)

        if isinstance(enhanced, list):
            self.log.info("生成 {} 个查询变体", len(enhanced))
        else:
            self.log.info("查询已增强")

        return enhanced

    def cleanup(self) -> None:
        """清理资源"""
        if self._strategy:
            self._strategy.cleanup()