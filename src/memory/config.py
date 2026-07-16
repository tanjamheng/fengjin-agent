"""记忆系统配置"""

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, model_validator
import yaml

load_dotenv()


class ChromaConfig(BaseModel):
    persist_directory: str = "data/chroma"
    collection_name: str = "memories"
    embedding_model: str = "models/bge-m3"
    device: str = "auto"


class ExtractionConfig(BaseModel):
    max_tokens: int = 2048
    prompt_file: str = "config/prompts/memory_extraction.md"


class MergeConfig(BaseModel):
    max_tokens: int = 512
    prompt_file: str = "config/prompts/memory_merge.md"


class ThresholdConfig(BaseModel):
    dedup_distance: float = 0.1
    conflict_distance: float = 0.15

    @model_validator(mode="after")
    def _check_threshold_order(self):
        if self.dedup_distance >= self.conflict_distance:
            raise ValueError(
                f"dedup_distance ({self.dedup_distance}) 必须小于 "
                f"conflict_distance ({self.conflict_distance})"
            )
        return self


class RetrievalConfig(BaseModel):
    top_k: int = 3


class FilterConfig(BaseModel):
    blacklist_patterns: List[str] = Field(default_factory=list)


class MemoryConfig(BaseModel):
    chroma: ChromaConfig = ChromaConfig()
    extraction: ExtractionConfig = ExtractionConfig()
    merge: MergeConfig = MergeConfig()
    thresholds: ThresholdConfig = ThresholdConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    core_file: str = "data/memory/core_memory.md"
    filter: FilterConfig = FilterConfig()


class MemorySettings(BaseModel):
    """记忆系统设置"""

    memory: MemoryConfig = MemoryConfig()

    @classmethod
    def load(cls, config_path: str = "config/memory.yaml") -> "MemorySettings":
        path = Path(config_path)
        if not path.exists():
            return cls()
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            return cls()
        return cls(memory=MemoryConfig(**data.get("memory", {})))

    @staticmethod
    def create_mind_model_client() -> OpenAI:
        """从环境变量创建心智模型 OpenAI 客户端。"""
        api_key = os.getenv("MIND_API_KEY")
        if not api_key:
            raise ValueError("请在 .env 文件中设置 MIND_API_KEY")
        base_url = os.getenv("MIND_BASE_URL")
        if not base_url:
            raise ValueError("请在 .env 文件中设置 MIND_BASE_URL")
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=45.0,
            # 由上层心智任务统一控制调用与结构校验重试次数，避免重试相乘。
            max_retries=0,
        )

    @staticmethod
    def get_mind_model_name() -> str:
        model = os.getenv("MIND_MODEL")
        if not model:
            raise ValueError("请在 .env 文件中设置 MIND_MODEL")
        return model
