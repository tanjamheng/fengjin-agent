"""记忆提取与过滤"""

import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from openai import BadRequestError, OpenAI

from .config import MemoryConfig, MemorySettings
from .storage import MemoryStorage
from .temporal import canonical_event_date
from ..mind.model_runtime import MindModelRuntime
from ..utils.logger import get_logger

MAX_PARSE_RETRIES = 3
_RELATIVE_TIME_PATTERN = re.compile(
    r"今天|今日|昨天|昨日|明天|明日|前天|后天|"
    r"这周|本周|上周|下周|这个月|本月|上个月|下个月|"
    r"今年|去年|明年|现在|目前|最近|近期|刚才|刚刚|这几天|前几天"
)
_TIME_SCOPES = {"timeless", "recurring", "temporary", "event"}
_DIRECT_DAY_OFFSETS = (
    (re.compile(r"前天"), -2),
    (re.compile(r"昨天|昨日"), -1),
    (re.compile(r"后天"), 2),
    (re.compile(r"明天|明日"), 1),
    (re.compile(r"今天|今日|现在|目前|刚才|刚刚"), 0),
)


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
                "5. 事实只能来自最新一轮用户明确说出的内容，风堇回复不是事实来源\n"
                "6. 问题、请求、假设及风堇自行补充的故事不能当作用户事实\n"
                "7. evidence 必须逐字复制最新一轮用户原话中的连续片段\n"
                "8. content 中的相对时间必须依据用户消息时间改写为绝对日期，不能保留“今天/昨天/明天”等说法\n"
                "重要性判断：high=过敏/禁忌/核心偏好/身份/重要事件（必须记住）；low=日常习惯/近期状态/一般偏好（按需检索）\n"
                "type说明：semantic=一般性知识/偏好，episodic=具体事件/经历。\n"
                "time_scope说明：timeless=长期事实，recurring=周期事件，temporary=短期状态，event=一次性事件。\n"
                "返回 JSON：{\"facts\": [{\"content\": \"...\", \"evidence\": \"用户原话\", \"type\": \"semantic|episodic\", \"importance\": \"high|low\", \"event_time\": \"YYYY-MM-DD或null\", \"time_scope\": \"timeless|recurring|temporary|event\"}]}"
            )
        self._blacklist = [
            re.compile(p) for p in config.filter.blacklist_patterns
        ]

    def extract(
        self,
        user_input: str,
        assistant_message: str,
        trace_id: str = "",
        source_timestamp: str | None = None,
    ) -> list[dict]:
        """提取记忆，返回过滤后的事实列表

        Returns:
            [{"content": str, "type": str, "importance": str}, ...]
        """
        log = self.log.bind(trace_id=trace_id) if trace_id else self.log
        facts = self._llm_extract(
            user_input, assistant_message, source_timestamp=source_timestamp
        )
        if not facts:
            return []
        return self._rule_filter(facts)

    def extract_conversation(self, conversation_text: str, trace_id: str = "",
                             runtime_lease=None,
                             source_timestamp: str | None = None) -> list[dict]:
        """从归一化后的最近多轮对话提取最新一轮产生的新事实。"""
        facts = self._llm_extract_text(
            conversation_text,
            runtime_lease,
            source_timestamp=source_timestamp,
        )
        return self._rule_filter(facts) if facts else []

    def _llm_extract(
        self,
        user_input: str,
        assistant_message: str,
        source_timestamp: str | None = None,
    ) -> list[dict]:
        """调用小模型提取事实，json_object 强制 JSON 输出"""
        conversation_text = f"用户：{user_input}\n风堇：{assistant_message}"
        return self._llm_extract_text(
            conversation_text, source_timestamp=source_timestamp
        )

    def _llm_extract_text(
        self,
        conversation_text: str,
        runtime_lease=None,
        source_timestamp: str | None = None,
    ) -> list[dict]:
        """调用心智模型提取事实，历史只用于理解最新一轮。"""
        if runtime_lease is None:
            with self.runtime.acquire("memory") as lease:
                return self._llm_extract_text(
                    conversation_text,
                    lease,
                    source_timestamp=source_timestamp,
                )

        lease = runtime_lease
        source_timestamp = source_timestamp or datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        latest_user_text = _extract_latest_user_text(conversation_text)
        messages = [
            {"role": "system", "content": self._extraction_prompt},
            {"role": "user", "content": (
                conversation_text
                + "\n\n只提取标记为‘最新一轮’中新出现或更新的用户事实；历史语境仅用于理解指代。"
                + f"\n最新一轮用户消息发生时间：{source_timestamp}。"
                + "\n若原话含相对时间，content 必须按该时间改写成绝对日期；evidence 仍逐字保留原话。"
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
                facts, error = self._parse_and_validate(
                    raw_text,
                    latest_user_text,
                    source_timestamp=source_timestamp,
                )
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

    def _parse_and_validate(
        self,
        text: str,
        latest_user_text: str,
        source_timestamp: str | None = None,
    ) -> tuple[list[dict] | None, str]:
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

            raw_content = fact.get("content", "")
            if not isinstance(raw_content, str):
                return None, "fact.content必须是字符串"
            content = raw_content.strip()
            if not content:
                continue

            evidence = fact.get("evidence", "")
            if not isinstance(evidence, str) or not evidence.strip():
                return None, "fact.evidence必须是非空字符串"
            if _normalize_evidence(evidence) not in _normalize_evidence(latest_user_text):
                return None, "fact.evidence必须逐字来自最新一轮用户原话"

            fact_type = fact.get("type", "")
            if fact_type not in ("semantic", "episodic"):
                fact_type = "semantic"

            importance = fact.get("importance", "")
            if importance not in ("high", "low"):
                importance = "low"

            if _RELATIVE_TIME_PATTERN.search(content):
                return None, "fact.content中的相对时间必须转换为绝对日期"

            event_time = fact.get("event_time")
            parsed_event_time: datetime | None = None
            if event_time is not None:
                if not isinstance(event_time, str):
                    return None, "fact.event_time必须是ISO日期字符串或null"
                event_time = event_time.strip()
                try:
                    parsed_event_time = datetime.fromisoformat(event_time)
                except ValueError:
                    return None, "fact.event_time必须使用YYYY-MM-DD或ISO日期时间"

            time_scope = fact.get("time_scope", "timeless")
            if time_scope not in _TIME_SCOPES:
                return None, "fact.time_scope必须是timeless、recurring、temporary或event"
            if time_scope != "timeless" and not event_time:
                return None, "非timeless记忆必须提供event_time"

            if _RELATIVE_TIME_PATTERN.search(evidence):
                if "time_scope" not in fact or "event_time" not in fact:
                    return None, "原话含相对时间时必须明确提供time_scope和event_time"
                if time_scope == "timeless" or parsed_event_time is None:
                    return None, "原话含相对时间时不能保存为无时间的timeless记忆"
                time_error = _validate_relative_event_time(
                    evidence,
                    source_timestamp,
                    parsed_event_time,
                )
                if time_error:
                    return None, time_error

            valid_facts.append({
                "content": content,
                "type": fact_type,
                "importance": importance,
                "event_time": event_time or "",
                "time_scope": time_scope,
                "source_timestamp": source_timestamp or "",
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
                where=None,
            )
            if results["distances"] and results["distances"][0]:
                distance = results["distances"][0][0]
                if distance < self.config.thresholds.dedup_distance:
                    metadata = results["metadatas"][0][0]
                    if _is_distinct_temporal_occurrence(fact, metadata):
                        filtered.append(fact)
                        continue
                    # 已降级 high 再次被判为 high 时交给 Writer 直接提升；
                    # 其余跨层/同层重复仍然丢弃。
                    if not (
                        fact["importance"] == "high"
                        and not metadata.get("is_core", 0)
                    ):
                        continue

            filtered.append(fact)

        return filtered


def _extract_latest_user_text(conversation_text: str) -> str:
    """从格式化对话中取最后一段用户原话，供证据硬校验。"""
    matches = re.findall(
        r"(?:^|\n)用户：(.*?)(?=\n风堇：|\Z)",
        conversation_text,
        flags=re.DOTALL,
    )
    return matches[-1].strip() if matches else ""


def _normalize_evidence(text: str) -> str:
    return re.sub(r"\s+", "", text.strip())


def _validate_relative_event_time(
    evidence: str,
    source_timestamp: str | None,
    event_time: datetime,
) -> str:
    """对可确定换算的相对日期做代码侧交叉校验。"""
    if not source_timestamp:
        return "缺少用户消息时间，无法校验相对日期"
    try:
        source_time = datetime.fromisoformat(source_timestamp)
    except ValueError:
        return "用户消息时间不是合法ISO日期时间"

    day_anchors = [
        offset
        for pattern, offset in _DIRECT_DAY_OFFSETS
        if pattern.search(evidence)
    ]
    year_anchors = [
        offset
        for pattern, offset in (
            (r"今年", 0),
            (r"去年", -1),
            (r"明年", 1),
        )
        if re.search(pattern, evidence)
    ]

    # 单一日锚点可无歧义换算；多个日锚点（如“昨天决定今天去”）
    # 无法仅靠正则判断事实指向哪一个，保留给模型理解，避免硬规则误杀。
    if len(day_anchors) == 1 and not year_anchors:
        expected = source_time.date() + timedelta(days=day_anchors[0])
        if event_time.date() != expected:
            return f"fact.event_time与原话相对日期不一致，应为{expected.isoformat()}"
        return ""

    # “去年/明年/今年 + 今天”是唯一安全的复合锚点。
    if (
        len(day_anchors) == 1
        and day_anchors[0] == 0
        and len(year_anchors) == 1
    ):
        try:
            expected = source_time.date().replace(
                year=source_time.year + year_anchors[0]
            )
        except ValueError:
            # 2月29日跨到非闰年时没有唯一公历日期，交由模型按语义处理。
            return ""
        if event_time.date() != expected:
            return f"fact.event_time与原话相对日期不一致，应为{expected.isoformat()}"
        return ""

    if not day_anchors and len(year_anchors) == 1:
        expected_year = source_time.year + year_anchors[0]
        if event_time.year != expected_year:
            return f"fact.event_time年份应为{expected_year}"
    return ""


def _is_distinct_temporal_occurrence(fact: dict, metadata: dict) -> bool:
    """时间类型或具体事件日期不同的事实不能仅凭向量相近判为重复。"""
    new_scope = fact.get("time_scope", "timeless")
    old_scope = metadata.get("time_scope", "timeless")
    if new_scope != old_scope and (
        new_scope != "timeless" or old_scope != "timeless"
    ):
        return True
    if new_scope in {"temporary", "event"}:
        new_time = canonical_event_date(fact.get("event_time"))
        old_time = canonical_event_date(metadata.get("event_time"))
        return bool(new_time or old_time) and new_time != old_time
    return False
