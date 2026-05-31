"""LLM 重排序策略

使用 LLM 判断每个文档与查询的相关性并排序。
最准确但成本最高，速度最慢。
"""

from typing import List
from .base import RerankerStrategy
from ..retriever.base import SearchResult


class LLMReranker(RerankerStrategy):
    """LLM 重排序"""

    RERANK_PROMPT = """请判断以下文档与查询的相关性。

查询：{query}

文档：{document}

请回答：
1. 相关（文档直接回答了查询）
2. 部分相关（文档包含相关信息但不完整）
3. 不相关（文档与查询无关）

只回答一个词：相关、部分相关、不相关"""

    RELEVANCE_SCORES = {
        "相关": 1.0,
        "部分相关": 0.5,
        "不相关": 0.0
    }

    def __init__(
        self,
        llm_client=None,
        llm_model: str = "glm-5",
        top_n: int = 3
    ):
        """
        Args:
            llm_client: LLM 客户端
            llm_model: 模型名称
            top_n: 返回数量
        """
        self.llm_client = llm_client
        self.llm_model = llm_model
        self.top_n = top_n

    def initialize(self) -> None:
        """无需初始化"""
        pass

    def _score_document(self, query: str, document: str) -> float:
        """用 LLM 判断相关性"""
        if self.llm_client is None:
            return 0.5  # 无 LLM 时返回中等分数

        try:
            prompt = self.RERANK_PROMPT.format(query=query, document=document[:500])
            response = self.llm_client.chat.completions.create(
                model=self.llm_model,
                max_tokens=10,
                messages=[{"role": "user", "content": prompt}]
            )
            answer = response.choices[0].message.content.strip()

            # 解析答案
            for key, score in self.RELEVANCE_SCORES.items():
                if key in answer:
                    return score

            return 0.5  # 无法解析时返回中等分数

        except Exception:
            return 0.5

    def rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        """LLM 重排序"""
        if not results:
            return results

        # 对每个文档打分
        scored_results = []
        for result in results:
            score = self._score_document(query, result.content)
            scored_results.append(SearchResult(
                content=result.content,
                score=score,
                metadata={**result.metadata, "rerank_method": "llm"},
                source=result.source
            ))

        # 排序
        scored_results.sort(key=lambda x: x.score, reverse=True)

        # 返回 top_n
        return scored_results[:self.top_n]

    def cleanup(self) -> None:
        """无需清理"""
        pass