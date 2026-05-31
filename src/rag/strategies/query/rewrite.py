"""查询改写策略

用 LLM 将模糊、口语化的查询改写为更清晰、具体的表述。
"""

from typing import Union, List
from .base import QueryEnhancerStrategy


class RewriteEnhancer(QueryEnhancerStrategy):
    """查询改写"""

    REWRITE_PROMPT = """请将以下用户查询改写为一个更清晰、更具体的表述。
改写后的查询应该：
1. 保持原意
2. 补充必要的上下文信息
3. 使用更专业、更准确的表述

原始查询：{query}

改写后的查询："""

    def __init__(
        self,
        llm_client=None,
        llm_model: str = "glm-5"
    ):
        """
        Args:
            llm_client: LLM 客户端
            llm_model: 模型名称
        """
        self.llm_client = llm_client
        self.llm_model = llm_model

    def initialize(self) -> None:
        """无需初始化"""
        pass

    def enhance(self, query: str) -> Union[str, List[str]]:
        """改写查询"""
        if self.llm_client is None:
            return query

        try:
            prompt = self.REWRITE_PROMPT.format(query=query)
            response = self.llm_client.chat.completions.create(
                model=self.llm_model,
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}]
            )
            rewritten = response.choices[0].message.content.strip()

            # 如果改写结果太短或无效，返回原查询
            if len(rewritten) < 5:
                return query

            return rewritten

        except Exception:
            return query

    def cleanup(self) -> None:
        """无需清理"""
        pass