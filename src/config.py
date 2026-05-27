"""配置管理模块

分工明确：
- .env: API Key等敏感信息
- config.yaml: Agent参数等非敏感配置
"""

import os
from dotenv import load_dotenv
import yaml
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Dict, Any

# 加载环境变量
load_dotenv()


class AgentConfig(BaseModel):
    """Agent 配置"""
    name: str = "SimpleAgent"
    max_tokens: int = 4096
    temperature: float = 0.7
    thinking_enabled: bool = False


class LoaderConfig(BaseModel):
    """文档加载配置"""
    supported_formats: List[str] = ["pdf", "md", "txt", "docx"]
    max_file_size_mb: int = 50


class SplitterConfig(BaseModel):
    """分块策略配置"""
    type: str = "recursive"
    params: Dict[str, Any] = Field(default_factory=lambda: {
        "chunk_size": 512,
        "chunk_overlap": 80
    })


class IndexConfig(BaseModel):
    """索引策略配置"""
    type: str = "hybrid"
    params: Dict[str, Any] = Field(default_factory=lambda: {
        "embedding_model": "BAAI/bge-m3",
        "persist_directory": "data/chroma",
        "collection_name": "fengjin_knowledge",
        "store_type": "chroma",
        "device": "cpu",
        "dense_weight": 0.7,
        "sparse_weight": 0.3
    })


class RetrieverConfig(BaseModel):
    """检索策略配置"""
    type: str = "hybrid"
    params: Dict[str, Any] = Field(default_factory=lambda: {
        "top_k": 8,
        "score_threshold": 0.0,
        "rrf_k": 60
    })


class RerankerConfig(BaseModel):
    """重排序策略配置"""
    type: str = "cross_encoder"
    params: Dict[str, Any] = Field(default_factory=lambda: {
        "model": "BAAI/bge-reranker-v2-m3",
        "top_n": 4
    })


class QueryEnhancerConfig(BaseModel):
    """查询增强策略配置"""
    type: str = "rewrite"
    params: Dict[str, Any] = Field(default_factory=lambda: {
        "llm_model": "glm-5"
    })


class RAGConfig(BaseModel):
    """RAG 配置"""
    splitter: SplitterConfig = SplitterConfig()
    index: IndexConfig = IndexConfig()
    retriever: RetrieverConfig = RetrieverConfig()
    reranker: RerankerConfig = RerankerConfig()
    query_enhancer: QueryEnhancerConfig = QueryEnhancerConfig()
    loader: LoaderConfig = LoaderConfig()


class SlidingWindowConfig(BaseModel):
    """滑动窗口配置"""
    max_turns: int = 20
    max_tokens: int = 4000


class MemoryMergeConfig(BaseModel):
    """记忆合并配置"""
    enabled: bool = True
    template: str = "[关于灰宝的信息]\n{memory}\n\n{input}"


class ContextConfig(BaseModel):
    """上下文管理配置"""
    sliding_window: SlidingWindowConfig = SlidingWindowConfig()
    memory: MemoryMergeConfig = MemoryMergeConfig()


class ContextSettings(BaseModel):
    """上下文管理设置"""

    context: ContextConfig = ContextConfig()

    @classmethod
    def load(cls, config_path: str = "config/context.yaml") -> "ContextSettings":
        path = Path(config_path)
        if not path.exists():
            return cls()
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(context=ContextConfig(**data.get("context", {})))


class Config(BaseModel):
    """全局配置（不含敏感信息）"""
    agent: AgentConfig
    system_prompt: str

    # API配置从环境变量读取（不在配置文件中）
    @property
    def api_key(self) -> str:
        """从环境变量获取API Key"""
        key = os.getenv("FENGJIN_API_KEY")
        if not key:
            raise ValueError("请在 .env 文件中设置 FENGJIN_API_KEY")
        return key

    @property
    def base_url(self) -> str:
        """从环境变量获取Base URL"""
        return os.getenv("FENGJIN_BASE_URL", "")

    @property
    def model(self) -> str:
        """从环境变量获取模型名称"""
        model = os.getenv("FENGJIN_MODEL")
        if not model:
            raise ValueError("请在 .env 文件中设置 FENGJIN_MODEL")
        return model

    @classmethod
    def load(cls, config_path: str = "config/config.yaml") -> "Config":
        """从 YAML 文件加载配置

        system_prompt 支持两种方式：
        1. 直接写字符串
        2. 用 system_prompt_file 指向外部 .md 文件
        """
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(
                f"配置文件不存在: {config_path}\n"
                "请复制 config/config.example.yaml 为 config.yaml"
            )

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # 加载 system prompt：优先从外部文件读取
        prompt = ""
        prompt_file = data.get("system_prompt_file")
        if prompt_file:
            p = Path(prompt_file)
            if not p.is_absolute():
                p = path.parent / p
            if p.exists():
                prompt = p.read_text(encoding="utf-8").strip()
        if not prompt:
            prompt = data.get("system_prompt", "你是一个有帮助的AI助手。")

        return cls(
            agent=AgentConfig(**data.get("agent", {})),
            system_prompt=prompt
        )


class RAGSettings(BaseModel):
    """RAG 设置"""

    rag: RAGConfig

    @classmethod
    def load(cls, config_path: str = "config/rag.yaml") -> "RAGSettings":
        """从 YAML 文件加载 RAG 配置"""
        path = Path(config_path)
        if not path.exists():
            return cls(rag=RAGConfig())

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        return cls(rag=RAGConfig(**data.get("rag", {})))