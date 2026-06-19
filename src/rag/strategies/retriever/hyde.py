"""HyDE 检索策略

Hypothetical Document Embeddings：
1. 用 LLM 生成假设回答文档
2. 对假设文档做 embedding
3. 用假设文档 embedding 检索真实文档

适用场景：用户提问表述与文档表述差异大的情况。
"""

from typing import List
from .base import RetrieverStrategy, SearchResult
from ....utils.logger import get_logger


class HyDERetriever(RetrieverStrategy):
    """HyDE 检索"""

    # 生成假设文档的 Prompt
    HYPOTHESIS_PROMPT = """请根据以下问题，生成一个假设的回答文档。
这个回答不一定是正确的，但应该包含与问题相关的关键词和概念。
回答应该详细一些，大约100-200字。

问题：{query}

假设回答："""

    def __init__(
        self,
        index,
        top_k: int = 5,
        score_threshold: float = 0.7,
        num_hypotheses: int = 3,
        llm_client=None,
        llm_model: str = "glm-5"
    ):
        """
        Args:
            index: 索引实例
            top_k: 返回数量
            score_threshold: 分数阈值
            num_hypotheses: 生成的假设文档数量
            llm_client: LLM 客户端（OpenAI 兼容）
            llm_model: 使用的模型
        """
        self.index = index
        self.top_k = top_k
        self.score_threshold = score_threshold
        self.num_hypotheses = num_hypotheses
        self.llm_client = llm_client
        self.llm_model = llm_model
        self.log = get_logger("hyde_retriever")

    def initialize(self) -> None:
        """初始化索引"""
        self.index.initialize()

    def _generate_hypotheses(self, query: str) -> List[str]:
        """生成假设文档"""
        hypotheses = []

        if self.llm_client is None:
            # 没有 LLM 客户端，使用原始查询
            return [query]

        for i in range(self.num_hypotheses):
            try:
                # 构建不同角度的提示
                prompts = [
                    self.HYPOTHESIS_PROMPT.format(query=query),
                    f"请从专业角度回答以下问题（可以是假设性的）：{query}",
                    f"请用通俗语言解释以下问题的答案（可以是假设性的）：{query}"
                ]
                prompt = prompts[i % len(prompts)]

                response = self.llm_client.chat.completions.create(
                    model=self.llm_model,
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}]
                )
                hypothesis = response.choices[0].message.content
                hypotheses.append(hypothesis)

            except Exception as e:
                self.log.error("HyDE假设生成失败: {}", e)
                hypotheses.append(query)

        # 当所有LLM调用失败时降级：去重后仅保留一份原始query
        if all(h == query for h in hypotheses):
            return [query]
        return hypotheses

    def retrieve(self, query: str) -> List[SearchResult]:
        """HyDE 检索"""
        # 生成假设文档
        hypotheses = self._generate_hypotheses(query)

        # 对每个假设文档检索
        all_results = []
        for hypothesis in hypotheses:
            raw_results = self.index.search(hypothesis, top_k=self.top_k)
            for item in raw_results:
                score = self._convert_score(item.get("distance", 0))
                item["hyde_score"] = score
                all_results.append(item)

        # 去重并排序
        seen = set()
        unique_results = []
        for item in all_results:
            content = item["content"]
            if content not in seen:
                seen.add(content)
                unique_results.append(item)

        # 按分数排序
        unique_results.sort(key=lambda x: x["hyde_score"], reverse=True)

        # 返回结果
        results = []
        for item in unique_results[:self.top_k]:
            if item["hyde_score"] >= self.score_threshold:
                results.append(SearchResult(
                    content=item["content"],
                    score=item["hyde_score"],
                    metadata=item.get("metadata", {}),
                    source=item.get("metadata", {}).get("file_name", "")
                ))

        return results

    def _convert_score(self, distance: float) -> float:
        """转换分数"""
        return 1.0 / (1.0 + distance)

    def cleanup(self) -> None:
        """清理"""
        self.index.cleanup()