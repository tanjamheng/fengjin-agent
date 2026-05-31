"""查询扩展策略

生成多个查询变体，从不同角度检索。

适用场景：
- 用户使用的词汇可能与文档不同
- 需要覆盖更多检索角度
"""

from typing import Union, List
from .base import QueryEnhancerStrategy


class ExpandEnhancer(QueryEnhancerStrategy):
    """查询扩展（多查询）"""

    EXPAND_PROMPT = """请为以下查询生成3个不同的表述变体。
变体应该：
1. 使用不同的词汇和表述方式
2. 从不同角度表达相同的意思
3. 包含可能的相关关键词

原始查询：{query}

请列出变体（每行一个）："""

    def __init__(
        self,
        llm_client=None,
        llm_model: str = "glm-5",
        num_variations: int = 3
    ):
        """
        Args:
            llm_client: LLM 客户端
            llm_model: 模型名称
            num_variations: 变体数量
        """
        self.llm_client = llm_client
        self.llm_model = llm_model
        self.num_variations = num_variations

    def initialize(self) -> None:
        """无需初始化"""
        pass

    def enhance(self, query: str) -> Union[str, List[str]]:
        """扩展查询"""
        if self.llm_client is None:
            return query

        try:
            prompt = self.EXPAND_PROMPT.format(query=query)
            response = self.llm_client.chat.completions.create(
                model=self.llm_model,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.choices[0].message.content.strip()

            # 解析变体
            variations = []
            for line in text.split('\n'):
                line = line.strip()
                # 移除编号前缀
                for prefix in ['1.', '2.', '3.', '1)', '2)', '3)', '-', '*']:
                    if line.startswith(prefix):
                        line = line[len(prefix):].strip()
                if line and len(line) > 5:
                    variations.append(line)

            # 限制数量并包含原查询
            variations = variations[:self.num_variations]
            variations.insert(0, query)  # 保留原查询

            return variations

        except Exception:
            return query

    def cleanup(self) -> None:
        """无需清理"""
        pass