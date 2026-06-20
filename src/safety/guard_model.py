"""P1: Llama Guard 3 1B 语义安全检测"""

import contextlib
import os
import threading
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel

from ..utils.logger import get_logger
from .rule_engine import Action, SafetyResult


# Llama Guard 类别 → 统一类别映射
# 格式: (category_id, category_name, action)
GUARD_TO_USER_CATEGORY: dict[str, tuple[str, str, Action]] = {
    "S1":  ("harmful_content", "违法有害内容", Action.BLOCK),
    "S2":  ("harmful_content", "违法有害内容", Action.BLOCK),
    "S3":  ("harmful_content", "违法有害内容", Action.BLOCK),
    "S4":  ("harmful_content", "违法有害内容", Action.BLOCK),
    "S5":  ("insult", "侮辱谩骂", Action.BLOCK),
    "S6":  ("harmful_content", "违法有害内容", Action.BLOCK),
    "S7":  ("privacy_request", "隐私泄露", Action.BLOCK),
    "S8":  ("harmful_content", "违法有害内容", Action.BLOCK),
    "S9":  ("harmful_content", "违法有害内容", Action.BLOCK),
    "S10": ("misogyny", "歧视与仇恨", Action.BLOCK),
    "S11": ("self_harm", "自杀与自伤", Action.COMFORT),
    "S12": ("harmful_content", "违法有害内容", Action.BLOCK),
}


class GuardModelConfig(BaseModel):
    """P1 模型配置"""
    model_config = {"protected_namespaces": ()}

    enabled: bool = False
    model_id: str = "LLM-Research/Llama-Guard-3-1B"
    source: str = "modelscope"  # modelscope / huggingface
    device: str = "auto"
    dtype: str = "bfloat16"
    lazy_load: bool = True
    timeout: float = 5.0
    fallback: str = "pass"


class GuardModel:
    """P1: Llama Guard 3 1B 语义安全检测"""

    def __init__(self, config_path: str):
        self.log = get_logger("guard_model")
        config_dir = Path(config_path).parent

        # 一次性读取 safety.yaml，避免重复 I/O 和 YAML 解析
        _safety = self._load_safety_data(config_path)

        raw = _safety.get("guard_model", {})
        self.config = GuardModelConfig(**raw)
        self.enabled = self.config.enabled

        if not self.enabled:
            self.log.info("P1 Llama Guard 已禁用")
            return

        # 从已缓存的 _safety dict 提取各配置段
        categories = _safety.get("categories", {})
        self._category_messages = {
            cat_id: cat.get("user_message", "")
            for cat_id, cat in categories.items()
            if cat.get("user_message")
        }
        self._default_message = _safety.get(
            "default_user_message", "该内容已被安全系统拦截。"
        )
        comfort = _safety.get("comfort", {})
        self._comfort_prompt = comfort.get("self_harm_prompt", "")

        # 延迟加载
        self._model = None
        self._tokenizer = None
        self._device = None
        self._loaded = False
        self._lock = threading.Lock()

        if not self.config.lazy_load:
            try:
                self._ensure_loaded()
            except Exception as e:
                self.log.error("Llama Guard 模型加载失败，P1 语义检测已禁用: {}", e)
                self.enabled = False

    def check(self, text: str, trace_id: str = "") -> SafetyResult:
        """语义安全检测"""
        log = self.log.bind(trace_id=trace_id) if trace_id else self.log
        if not self.enabled:
            return SafetyResult()

        try:
            self._ensure_loaded()
        except Exception as e:
            log.warning("Llama Guard 懒加载失败，P1 语义检测已禁用: {}", e)
            self.enabled = False
            return SafetyResult()

        try:
            # content 必须是 [{"type":"text","text":"..."}] 格式，
            # 否则 Jinja 模板的 selectattr('type', 'equalto', 'text') 匹配不到，对话区域为空
            messages = [{"role": "user", "content": [{"type": "text", "text": text}]}]
            input_ids = self._tokenizer.apply_chat_template(
                messages, return_tensors="pt",
            ).to(self._device)

            import torch
            attention_mask = torch.ones_like(input_ids)
            with torch.no_grad():
                output = self._model.generate(
                    input_ids,
                    attention_mask=attention_mask,
                    max_new_tokens=100,
                    max_time=self.config.timeout,
                    do_sample=False,
                    pad_token_id=self._tokenizer.eos_token_id,
                )

            prompt_len = input_ids.shape[1]
            output_text = self._tokenizer.decode(
                output[0][prompt_len:],
                skip_special_tokens=True,
            )

            return self._parse_output(output_text)

        except Exception as e:
            log.error("P1 检测失败: {}", e)
            if self.config.fallback == "pass":
                return SafetyResult()
            return SafetyResult(
                action=Action.BLOCK,
                category="guard_error",
                category_name="模型检测失败",
                severity="medium",
                match_type="guard_model",
                reason=f"P1 模型异常: {e}",
            )

    # ── 模型加载 ──────────────────────────────────────────

    def _ensure_loaded(self):
        """延迟加载模型（线程安全）"""
        if self._loaded:
            return

        with self._lock:
            if self._loaded:
                return

            self.log.info("正在加载 Llama Guard 3 1B ({})...", self.config.model_id)

            import torch
            from transformers import AutoTokenizer, AutoModelForCausalLM

            # 解析模型路径：支持 ModelScope 本地缓存 / HuggingFace / 本地路径
            model_path = self._resolve_model_path()

            self._tokenizer = AutoTokenizer.from_pretrained(model_path)

            dtype_map = {
                "bfloat16": torch.bfloat16,
                "float16": torch.float16,
                "float32": torch.float32,
            }
            torch_dtype = dtype_map.get(self.config.dtype, torch.bfloat16)

            # 抑制 transformers 加载时的 stderr 噪音（如 "meta device" 警告）
            with open(os.devnull, "w") as devnull:
                with contextlib.redirect_stderr(devnull):
                    self._model = AutoModelForCausalLM.from_pretrained(
                        model_path,
                        torch_dtype=torch_dtype,
                        device_map=self.config.device,
                    )
            self._device = self._model.device

            self._loaded = True
            self.log.info("Llama Guard 3 1B 加载完成")

    def cleanup(self) -> None:
        """释放 GPU 模型和显存（线程安全）"""
        if not self.enabled:
            return
        with self._lock:
            if self._model is not None:
                del self._model
                self._model = None
            if self._tokenizer is not None:
                del self._tokenizer
                self._tokenizer = None
            self._loaded = False
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception as e:
            self.log.warning("CUDA缓存清理异常: {}", e)
        self.log.info("Llama Guard 模型已释放")

    # ── 输出解析 ──────────────────────────────────────────

    def _parse_output(self, output_text: str) -> SafetyResult:
        """解析 'safe' / 'unsafe S1 S10' 等输出"""
        text = output_text.strip().lower()

        if text.startswith("safe"):
            return SafetyResult(match_type="guard_model")

        if not text.startswith("unsafe"):
            return SafetyResult(match_type="guard_model")

        # 提取违反的类别编号
        categories = []
        for part in text.split():
            part = part.strip().strip(",").upper()
            if part.startswith("S") and part[1:].isdigit():
                categories.append(part)

        if not categories:
            return SafetyResult(
                action=Action.BLOCK,
                category="unknown_guard",
                category_name="Llama Guard 拦截",
                severity="high",
                match_type="guard_model",
                reason="语义检测判定为不安全（类别未知）",
            )

        # 映射到用户分类（取第一个匹配的）
        for cat in categories:
            if cat in GUARD_TO_USER_CATEGORY:
                user_cat, cat_name, action = GUARD_TO_USER_CATEGORY[cat]
                user_msg = self._category_messages.get(
                    user_cat, self._default_message
                )
                result = SafetyResult(
                    action=action,
                    category=user_cat,
                    category_name=cat_name,
                    severity="high",
                    match_type="guard_model",
                    matched_pattern=cat,
                    reason=f"语义检测 [{cat_name}]: Llama Guard {cat}",
                    user_message=user_msg,
                )
                if action == Action.COMFORT and self._comfort_prompt:
                    result.comfort_prompt = self._comfort_prompt
                return result

        # 类别不在映射表中
        return SafetyResult(
            action=Action.BLOCK,
            category="unknown",
            category_name="未知违规",
            severity="high",
            match_type="guard_model",
            matched_pattern=", ".join(categories),
            reason=f"语义检测: Llama Guard {', '.join(categories)}",
            user_message=self._default_message,
        )

    # ── 配置加载 ──────────────────────────────────────────

    def _resolve_model_path(self) -> str:
        """解析模型路径：本地路径 / ModelScope 缓存 / HuggingFace"""
        model_id = self.config.model_id

        if self.config.source == "local":
            path = Path(model_id)
            if not path.is_absolute():
                path = Path(__file__).parent.parent.parent / path
            if not path.exists():
                raise FileNotFoundError(f"本地模型路径不存在: {path}")
            self.log.info("从本地加载: {}", path)
            return str(path)

        if self.config.source == "modelscope":
            from modelscope.hub.snapshot_download import snapshot_download
            cache_dir = snapshot_download(model_id)
            self.log.info("从 ModelScope 加载: {}", cache_dir)
            return cache_dir

        # huggingface 源：model_id 直接传给 transformers
        return model_id

    @staticmethod
    def _load_safety_data(config_path: str) -> dict:
        """一次性读取 safety.yaml，返回完整的 safety 配置段（避免重复 I/O）"""
        path = Path(config_path)
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            return {}
        return data.get("safety", {})

    @staticmethod
    def _load_guard_config(config_path: str) -> dict:
        return GuardModel._load_safety_data(config_path).get("guard_model", {})

    @staticmethod
    def _load_category_messages(config_path: str) -> dict[str, str]:
        categories = GuardModel._load_safety_data(config_path).get("categories", {})
        return {
            cat_id: cat.get("user_message", "")
            for cat_id, cat in categories.items()
            if cat.get("user_message")
        }

    @staticmethod
    def _load_default_message(config_path: str) -> str:
        return GuardModel._load_safety_data(config_path).get(
            "default_user_message", "该内容已被安全系统拦截。"
        )

    @staticmethod
    def _load_comfort_prompt(config_path: str) -> str:
        comfort = GuardModel._load_safety_data(config_path).get("comfort", {})
        return comfort.get("self_harm_prompt", "")
