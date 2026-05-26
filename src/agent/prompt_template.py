"""Prompt 模板管理

管理各种 Prompt 模板，支持：
- 模板注册和获取
- 变量替换
- RAG 专用模板
"""

from typing import Dict, Optional
from pydantic import BaseModel


class PromptTemplate(BaseModel):
    """Prompt 模板"""
    name: str
    template: str
    variables: list = []
    description: str = ""


class PromptManager:
    """Prompt 模板管理器"""

    DEFAULT_TEMPLATES = {
        "rag": PromptTemplate(
            name="rag",
            template="""根据以下参考资料回答用户问题。

参考资料：
{context}

用户问题：{query}

请基于参考资料回答问题，如果参考资料中没有相关信息，请直接说明"根据提供的资料，我无法回答这个问题"。回答要简洁准确。""",
            variables=["context", "query"],
            description="RAG 上下文注入模板"
        ),
        "chat": PromptTemplate(
            name="chat",
            template="{query}",
            variables=["query"],
            description="基础对话模板"
        ),
        "multi_turn": PromptTemplate(
            name="multi_turn",
            template="""历史对话：
{history}

当前问题：{query}

请根据历史对话上下文回答当前问题。""",
            variables=["history", "query"],
            description="多轮对话模板"
        )
    }

    def __init__(self):
        self.templates: Dict[str, PromptTemplate] = {}
        self._load_defaults()

    def _load_defaults(self) -> None:
        """加载默认模板"""
        for name, template in self.DEFAULT_TEMPLATES.items():
            self.templates[name] = template

    def register(self, template: PromptTemplate) -> None:
        """注册模板"""
        self.templates[template.name] = template

    def get(self, name: str) -> Optional[PromptTemplate]:
        """获取模板"""
        return self.templates.get(name)

    def render(self, name: str, **kwargs) -> str:
        """渲染模板"""
        template = self.get(name)
        if template is None:
            raise ValueError(f"模板不存在: {name}")

        # 替换变量
        result = template.template
        for var in template.variables:
            if var in kwargs:
                result = result.replace(f"{{{var}}}", str(kwargs[var]))

        return result

    def list_templates(self) -> list:
        """列出所有模板"""
        return [{"name": t.name, "description": t.description, "variables": t.variables}
                for t in self.templates.values()]


def get_prompt_manager() -> PromptManager:
    """获取全局 PromptManager"""
    return PromptManager()