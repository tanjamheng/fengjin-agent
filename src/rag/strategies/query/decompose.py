"""查询分解策略

将复杂的多跳问题分解为多个简单的子问题。

适用场景：
- "A 和 B 有什么关系？" → ["A 是什么？", "B 是什么？", "A 和 B 的关系是什么？"]
- "为什么 X 导致 Y？" → ["X 是什么？", "Y 是什么？", "X 如何影响 Y？"]
"""

from typing import Union, List
from .base import QueryEnhancerStrategy
from ....utils.logger import get_logger


class DecomposeEnhancer(QueryEnhancerStrategy):
    """查询分解"""

    DECOMPOSE_PROMPT = """请将以下复杂问题分解为多个简单的子问题。
每个子问题应该是独立的、可以单独回答的。

复杂问题：{query}

请列出子问题（每行一个）："""

    def __init__(
        self,
        llm_client=None,
        llm_model: str = "glm-5",
        max_sub_questions: int = 3
    ):
        """
        Args:
            llm_client: LLM 客户端
            llm_model: 模型名称
            max_sub_questions: 最大子问题数量
        """
        self.llm_client = llm_client
        self.llm_model = llm_model
        self.max_sub_questions = max_sub_questions
        self.log = get_logger("query_decompose")

    def initialize(self) -> None:
        """无需初始化"""
        pass

    def enhance(self, query: str) -> Union[str, List[str]]:
        """分解查询"""
        if self.llm_client is None:
            return query

        try:
            prompt = self.DECOMPOSE_PROMPT.format(query=query)
            response = self.llm_client.chat.completions.create(
                model=self.llm_model,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.choices[0].message.content.strip()

            # 解析子问题
            sub_questions = []
            for line in text.split('\n'):
                line = line.strip()
                # 移除编号前缀
                for prefix in ['1.', '2.', '3.', '1)', '2)', '3)', '-', '*']:
                    if line.startswith(prefix):
                        line = line[len(prefix):].strip()
                if line and len(line) > 5:
                    sub_questions.append(line)

            # 限制数量
            sub_questions = sub_questions[:self.max_sub_questions]

            # 如果分解失败，返回原查询
            if not sub_questions:
                return query

            return sub_questions

        except Exception as e:
            self.log.error(f"查询分解失败: {e}")
            return query

    def cleanup(self) -> None:
        """无需清理"""
        pass