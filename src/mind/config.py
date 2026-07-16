"""心智模型配置。"""

import os
from pathlib import Path

import yaml
from openai import OpenAI
from pydantic import BaseModel, Field


class MindConfig(BaseModel):
    context_turns: int = Field(default=3, ge=1, le=10)
    state_max_tokens: int = Field(default=512, ge=128, le=4096)
    timeout_seconds: float = Field(default=45.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=5)
    cleanup_timeout_seconds: float = Field(default=10.0, gt=0)
    prompt_file: str = "config/prompts/state_analysis.md"


class MindSettings(BaseModel):
    mind: MindConfig = MindConfig()

    @classmethod
    def load(cls, config_path: str = "config/mind.yaml") -> "MindSettings":
        path = Path(config_path)
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(mind=MindConfig(**data.get("mind", {})))

    @staticmethod
    def validate_environment() -> None:
        missing = [
            key for key in ("MIND_API_KEY", "MIND_BASE_URL", "MIND_MODEL")
            if not os.getenv(key, "").strip()
        ]
        if missing:
            raise ValueError(f"请在 .env 文件中设置 {', '.join(missing)}")

    @staticmethod
    def create_client(config: MindConfig) -> OpenAI:
        MindSettings.validate_environment()
        return OpenAI(
            api_key=os.environ["MIND_API_KEY"],
            base_url=os.environ["MIND_BASE_URL"],
            timeout=config.timeout_seconds,
            max_retries=0,
        )

    @staticmethod
    def model_name() -> str:
        MindSettings.validate_environment()
        return os.environ["MIND_MODEL"]
