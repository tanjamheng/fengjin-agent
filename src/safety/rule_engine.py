"""规则引擎：纯关键词+正则匹配，无 LLM 调用"""

import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from ..utils.logger import get_logger
from .loaders import load_keywords, load_regex_patterns


# ── 数据结构 ──────────────────────────────────────────────


class Action(str, Enum):
    BLOCK = "block"
    COMFORT = "comfort"
    PASS = "pass"


@dataclass
class SafetyResult:
    """安全检查结果"""
    action: Action = Action.PASS
    category: Optional[str] = None
    category_name: Optional[str] = None
    severity: Optional[str] = None
    matched_pattern: Optional[str] = None
    match_type: Optional[str] = None
    reason: Optional[str] = None
    comfort_prompt: Optional[str] = None
    user_message: Optional[str] = None

    @property
    def passed(self) -> bool:
        """是否正常放行"""
        return self.action == Action.PASS

    @property
    def blocked(self) -> bool:
        """是否被拦截"""
        return self.action == Action.BLOCK


# ── 配置模型 ──────────────────────────────────────────────


class CategoryConfig(BaseModel):
    """单个规则类别配置"""
    name: str
    severity: str = "high"
    action: str = "block"
    enabled: bool = True
    user_message: str = ""


class MatchConfig(BaseModel):
    """匹配参数"""
    case_sensitive: bool = False
    check_invisible_chars: bool = True


class ComfortConfig(BaseModel):
    """comfort 模式配置"""
    self_harm_prompt: str = ""


class SafetyConfig(BaseModel):
    """安全护栏配置"""
    enabled: bool = True
    categories: dict[str, CategoryConfig] = Field(default_factory=dict)
    words_dir: str = "safety_words"
    match: MatchConfig = MatchConfig()
    comfort: ComfortConfig = ComfortConfig()
    default_user_message: str = "该内容已被安全系统拦截。"

    @classmethod
    def load(cls, config_path: str) -> "SafetyConfig":
        """从 YAML 文件加载配置"""
        path = Path(config_path)
        if not path.exists():
            return cls()
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            return cls()
        safety_data = data.get("safety", {})
        return cls(
            enabled=safety_data.get("enabled", True),
            categories={
                k: CategoryConfig(**v)
                for k, v in safety_data.get("categories", {}).items()
            },
            words_dir=safety_data.get("words_dir", "safety_words"),
            match=MatchConfig(**safety_data.get("match", {})),
            comfort=ComfortConfig(**safety_data.get("comfort", {})),
        )


# ── 规则引擎 ──────────────────────────────────────────────


class RuleEngine:
    """规则引擎：纯关键词+正则匹配，无 LLM 调用"""

    def __init__(self, config_path: str = "config/safety.yaml"):
        self.config = SafetyConfig.load(config_path)
        self.log = get_logger("safety_engine")

        if not self.config.enabled:
            self.log.info("安全护栏已禁用")
            self._keywords: dict[str, list[str]] = {}
            self._regex_patterns: list[dict] = []
            return

        # 词库路径 = 配置文件所在目录 / words_dir
        config_dir = Path(config_path).parent
        words_dir = config_dir / self.config.words_dir

        # 加载关键词和正则
        self._keywords = load_keywords(words_dir, self.config.categories)
        self._regex_patterns = load_regex_patterns(words_dir / "regex_patterns.yaml")

        total_keywords = sum(len(v) for v in self._keywords.values())
        self.log.info(
            "安全护栏初始化: {} 个关键词, {} 条正则, {} 个类别",
            total_keywords, len(self._regex_patterns),
            sum(1 for c in self.config.categories.values() if c.enabled)
        )

    def check(self, text: str) -> SafetyResult:
        """检查文本安全性

        匹配顺序：不可见字符 → 关键词 → 正则
        首次命中即返回（fail_fast）
        """
        if not self.config.enabled:
            return SafetyResult()

        if not text or not text.strip():
            return SafetyResult()

        # 1. 不可见字符检测
        if self.config.match.check_invisible_chars:
            result = self._check_invisible_chars(text)
            if result:
                return result

        # 2. 关键词匹配
        result = self._check_keywords(text)
        if result:
            return result

        # 3. 正则匹配
        result = self._check_regex(text)
        if result:
            return result

        return SafetyResult()

    def _check_invisible_chars(self, text: str) -> Optional[SafetyResult]:
        """检测不可见字符（零宽空格、私有区字符等）"""
        banned_categories = {"Cc", "Cf", "Co", "Cn"}
        found_chars = []

        for char in text:
            if unicodedata.category(char) in banned_categories:
                found_chars.append(f"U+{ord(char):04X}")

        if found_chars:
            return SafetyResult(
                action=Action.BLOCK,
                category="invisible_char",
                category_name="不可见字符",
                severity="critical",
                match_type="invisible_char",
                matched_pattern=", ".join(found_chars),
                reason="输入中包含不可见字符，疑似注入攻击",
            )
        return None

    def _check_keywords(self, text: str) -> Optional[SafetyResult]:
        """关键词匹配：遍历所有启用的类别"""
        search_text = text if self.config.match.case_sensitive else text.lower()

        for cat_id, cat_config in self.config.categories.items():
            if not cat_config.enabled:
                continue

            keywords = self._keywords.get(cat_id, [])
            for keyword in keywords:
                kw = keyword if self.config.match.case_sensitive else keyword.lower()
                if kw in search_text:
                    return self._build_result(cat_id, cat_config, "keyword", keyword)

        return None

    def _check_regex(self, text: str) -> Optional[SafetyResult]:
        """正则匹配：遍历所有预编译正则"""
        for item in self._regex_patterns:
            cat_id = item["category"]
            compiled = item["pattern"]
            raw = item["raw"]

            cat_config = self.config.categories.get(cat_id)
            if not cat_config or not cat_config.enabled:
                continue

            match = compiled.search(text)
            if match:
                return self._build_result(cat_id, cat_config, "regex", raw)

        return None

    def _build_result(
        self,
        cat_id: str,
        cat_config: CategoryConfig,
        match_type: str,
        matched_pattern: str,
    ) -> SafetyResult:
        """构建检查结果"""
        action = Action(cat_config.action)

        comfort_prompt = None
        if action == Action.COMFORT and cat_id == "self_harm":
            comfort_prompt = self.config.comfort.self_harm_prompt

        # 用户可见的拦截话术：优先用类别配置，否则用默认
        user_msg = cat_config.user_message or self.config.default_user_message

        return SafetyResult(
            action=action,
            category=cat_id,
            category_name=cat_config.name,
            severity=cat_config.severity,
            match_type=match_type,
            matched_pattern=matched_pattern,
            reason=f"触发规则 [{cat_config.name}]: {matched_pattern}",
            comfort_prompt=comfort_prompt,
            user_message=user_msg,
        )
