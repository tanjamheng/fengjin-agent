"""记忆提取与过滤"""

import json
import re
import threading
import time
from pathlib import Path

from openai import BadRequestError, OpenAI

from .config import MemoryConfig, MemorySettings
from .storage import MemoryStorage
from ..mind.model_runtime import MindModelRuntime
from ..utils.logger import get_logger

MAX_PARSE_RETRIES = 3


class MemoryModelOutputError(RuntimeError):
    """记忆模型持续返回无法校验的 JSON。"""


class MemoryExtractor:
    """记忆提取器

    两步处理：
    1. LLM 提取 + 过滤 + 重要性判断（语义层，OpenAI json_object 强制 JSON）
    2. 规则兜底：PII 黑名单 + 向量去重（代码层）
    """

    def __init__(self, config: MemoryConfig, client: OpenAI | None,
                 model: str, storage: MemoryStorage,
                 max_retries: int = MAX_PARSE_RETRIES,
                 *, runtime: MindModelRuntime | None = None):
        self.config = config
        if runtime is None:
            if client is None:
                raise ValueError("MemoryExtractor 需要 client 或 runtime")
            runtime = MindModelRuntime.single_client(client, model)
        self.runtime = runtime
        self.storage = storage
        self.max_retries = max_retries
        self._mode_lock = threading.Lock()
        self._response_modes: dict[int, str] = {}
        self.log = get_logger("memory_extractor")
        try:
            self._extraction_prompt = Path(config.extraction.prompt_file).read_text(
                encoding="utf-8"
            )
        except Exception as e:
            self.log.warning("记忆提取 prompt 文件读取失败，使用内嵌默认模板: {}", e)
            self._extraction_prompt = (
                "请从以下对话中提取关于用户（灰宝）值得记住的个人事实和偏好。\n"
                "注意：不要提取风堇自身的事实（她的设定已经完整定义），只提取用户的信息。\n"
                "提取规则：\n"
                "1. 只提取有价值的个人信息（偏好、习惯、重要事件、人际关系等）\n"
                "2. 跳过寒暄、闲聊、无实质内容的对话\n"
                "3. 跳过显而易见的常识\n"
                "4. 跳过过于具体且不重要的细节\n"
                "重要性判断：high=过敏/禁忌/核心偏好/身份/重要事件（必须记住）；low=日常习惯/近期状态/一般偏好（按需检索）\n"
                "type说明：semantic=一般性知识/偏好，episodic=具体事件/经历。\n"
                "返回 JSON：{\"facts\": [{\"content\": \"...\", \"type\": \"semantic|episodic\", \"importance\": \"high|low\"}]}"
            )
        self._blacklist = [
            re.compile(p) for p in config.filter.blacklist_patterns
        ]

    def extract(self, user_input: str, assistant_message: str, trace_id: str = "") -> list[dict]:
        """提取记忆，返回过滤后的事实列表

        Returns:
            [{"content": str, "type": str, "importance": str}, ...]
        """
        log = self.log.bind(trace_id=trace_id) if trace_id else self.log
        facts = self._llm_extract(user_input, assistant_message)
        if not facts:
            return []
        return self._rule_filter(facts)

    def extract_conversation(self, conversation_text: str, trace_id: str = "",
                             runtime_lease=None) -> list[dict]:
        """从归一化后的最近多轮对话提取最新一轮产生的新事实。"""
        facts = self._llm_extract_text(conversation_text, runtime_lease)
        return self._rule_filter(facts) if facts else []

    def _llm_extract(self, user_input: str, assistant_message: str) -> list[dict]:
        """调用小模型提取事实，json_object 强制 JSON 输出"""
        conversation_text = f"用户：{user_input}\n风堇：{assistant_message}"
        return self._llm_extract_text(conversation_text)

    def _llm_extract_text(self, conversation_text: str, runtime_lease=None) -> list[dict]:
        """调用心智模型提取事实，历史只用于理解最新一轮。"""
        if runtime_lease is None:
            with self.runtime.acquire("memory") as lease:
                return self._llm_extract_text(conversation_text, lease)

        lease = runtime_lease
        messages = [
            {"role": "system", "content": self._extraction_prompt},
            {"role": "user", "content": (
                conversation_text
                + "\n\n只提取标记为‘最新一轮’中新出现或更新的用户事实；历史语境仅用于理解指代。"
            )}
        ]

        last_error: Exception | None = None
        attempt = 0
        response_mode = self._get_response_mode(lease.version)
        while attempt <= self.max_retries:
            try:
                kwargs = dict(
                    model=lease.model,
                    max_tokens=self.config.extraction.max_tokens,
                    messages=messages,
                )
                if response_mode == "json_object":
                    kwargs["response_format"] = {"type": "json_object"}
                response = lease.client.chat.completions.create(**kwargs)
                raw_text = (response.choices[0].message.content or "").strip()
                facts, error = self._parse_and_validate(raw_text)
                if facts is not None:
                    return facts

                last_error = MemoryModelOutputError(error)
                if attempt < self.max_retries:
                    # 纠错历史只存在于本次任务，不污染下一轮心智分析。
                    messages.append({"role": "assistant", "content": raw_text})
                    messages.append({
                        "role": "user",
                        "content": f"返回的JSON格式有误：{error}\n请修正后重新返回，只返回合法JSON。"
                    })
            except BadRequestError as exc:
                lowered = str(exc).lower()
                unsupported_markers = (
                    "response_format", "json_object", "json mode", "structured output"
                )
                if response_mode == "json_object" and any(
                    marker in lowered for marker in unsupported_markers
                ):
                    response_mode = "prompt_only"
                    self._set_response_mode(lease.version, response_mode)
                    self.log.warning("供应商不支持 json_object，记忆提取降级为 prompt_only")
                    last_error = exc
                    # 能力协商不消耗业务重试次数；立即用 prompt-only 重发。
                    continue
                last_error = exc
                if getattr(exc, "status_code", None) in (401, 403, 404):
                    raise
            except Exception as exc:
                last_error = exc
                if getattr(exc, "status_code", None) in (401, 403, 404):
                    raise

            if attempt < self.max_retries:
                time.sleep(min(2 ** attempt, 4))
            attempt += 1

        self.log.warning("记忆提取调用或JSON校验失败，已重试 {} 次后放弃: {}", self.max_retries, last_error)
        if isinstance(last_error, MemoryModelOutputError):
            raise MemoryModelOutputError("记忆模型持续返回非法 JSON") from last_error
        if last_error is not None:
            raise last_error
        raise MemoryModelOutputError("记忆模型未返回可用结果")

    def _get_response_mode(self, version: int) -> str:
        with self._mode_lock:
            return self._response_modes.setdefault(version, "json_object")

    def _set_response_mode(self, version: int, mode: str) -> None:
        with self._mode_lock:
            self._response_modes[version] = mode

    def _parse_and_validate(self, text: str) -> tuple[list[dict] | None, str]:
        """解析 JSON 并校验字段完整性

        Returns:
            (facts_list, error_msg) — 成功时 error_msg 为空字符串
        """
        json_text = text
        if "```json" in text:
            json_text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            json_text = text.split("```")[1].split("```")[0].strip()

        try:
            result = json.loads(json_text)
        except json.JSONDecodeError as e:
            return None, f"JSON语法错误: {e}"

        if not isinstance(result, dict) or "facts" not in result:
            return None, "缺少facts字段"

        facts = result["facts"]
        if not isinstance(facts, list):
            return None, "facts必须是数组"

        valid_facts = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue

            content = fact.get("content", "").strip()
            if not content:
                continue

            fact_type = fact.get("type", "")
            if fact_type not in ("semantic", "episodic"):
                fact_type = "semantic"

            importance = fact.get("importance", "")
            if importance not in ("high", "low"):
                importance = "low"

            valid_facts.append({
                "content": content,
                "type": fact_type,
                "importance": importance
            })

        return valid_facts, ""

    def _rule_filter(self, facts: list[dict]) -> list[dict]:
        """规则兜底：PII 黑名单 + 向量去重"""
        filtered = []
        for fact in facts:
            content = fact.get("content", "")
            if not content:
                continue

            if any(pattern.search(content) for pattern in self._blacklist):
                continue

            results = self.storage.query(
                text=content,
                n_results=1,
                where={"is_core": 1 if fact["importance"] == "high" else 0}
            )
            if results["distances"] and results["distances"][0]:
                distance = results["distances"][0][0]
                if distance < self.config.thresholds.dedup_distance:
                    continue

            filtered.append(fact)

        return filtered
