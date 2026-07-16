"""心智模型状态分析：严格 JSON、带上下文纠错与有限重试。"""

import json
import time
from pathlib import Path

from openai import BadRequestError, OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import MindConfig
from .context_builder import format_turns
from ..utils.logger import get_logger


class MoodTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pleasure: float = Field(strict=True, allow_inf_nan=False, ge=-1.0, le=1.0)
    arousal: float = Field(strict=True, allow_inf_nan=False, ge=0.0, le=1.0)
    dominance: float = Field(strict=True, allow_inf_nan=False, ge=-1.0, le=1.0)


class BondTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warmth: float = Field(strict=True, allow_inf_nan=False, ge=0.0, le=1.0)
    trust: float = Field(strict=True, allow_inf_nan=False, ge=0.0, le=1.0)
    formality: float = Field(strict=True, allow_inf_nan=False, ge=0.0, le=1.0)
    humor: float = Field(strict=True, allow_inf_nan=False, ge=0.0, le=1.0)


class StateAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mood: MoodTarget
    bond: BondTarget


class MindModelError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


_JSON_SCHEMA = {
    "name": "fengjin_mind_state",
    "strict": True,
    "schema": StateAnalysisResult.model_json_schema(),
}


class StateAnalyzer:
    def __init__(self, config: MindConfig, client: OpenAI, model: str):
        self.config = config
        self.client = client
        self.model = model
        self.log = get_logger("mind_state")
        root = Path(__file__).resolve().parent.parent.parent
        prompt_path = Path(config.prompt_file)
        if not prompt_path.is_absolute():
            prompt_path = root / prompt_path
        self.prompt = prompt_path.read_text(encoding="utf-8")
        self._response_mode = "json_schema"

    def analyze(self, turns: list[dict], mood: dict, bond: dict,
                trace_id: str = "") -> StateAnalysisResult:
        log = self.log.bind(trace_id=trace_id) if trace_id else self.log
        payload = (
            f"更新前情绪：{json.dumps(_pick(mood, ('pleasure','arousal','dominance')), ensure_ascii=False)}\n"
            f"更新前羁绊：{json.dumps(_pick(bond, ('warmth','trust','formality','humor')), ensure_ascii=False)}\n\n"
            f"最近对话：\n{format_turns(turns)}"
        )
        messages = [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": payload},
        ]
        last_error = ""
        for attempt in range(self.config.max_retries + 1):
            try:
                kwargs = {
                    "model": self.model,
                    "messages": messages,
                    "max_tokens": self.config.state_max_tokens,
                    "temperature": 0.1,
                }
                response_format = self._response_format()
                if response_format is not None:
                    kwargs["response_format"] = response_format
                response = self.client.chat.completions.create(**kwargs)
                raw = (response.choices[0].message.content or "").strip()
                try:
                    result = StateAnalysisResult.model_validate_json(raw)
                    log.info("心智状态 JSON 校验通过 (attempt={})", attempt + 1)
                    return result
                except (ValidationError, ValueError) as exc:
                    last_error = _compact_validation_error(exc)
                    if attempt < self.config.max_retries:
                        messages.append({"role": "assistant", "content": raw[:4000]})
                        messages.append({
                            "role": "user",
                            "content": (
                                f"上一输出未通过 JSON Schema 校验：{last_error}\n"
                                "请修正并重新输出。只能输出符合 Schema 的 JSON，不要解释或使用 Markdown 代码块。"
                            ),
                        })
            except BadRequestError as exc:
                text = str(exc)
                lowered = text.lower()
                if self._response_mode in ("json_schema", "json_object") and (
                    "response_format" in lowered or "json_schema" in lowered
                ):
                    previous_mode = self._response_mode
                    self._response_mode = (
                        "json_object" if previous_mode == "json_schema" else "prompt_only"
                    )
                    log.warning("供应商不支持 {}，降级为 {}", previous_mode, self._response_mode)
                    last_error = text
                    continue
                last_error = text
                if _is_permanent_error(exc):
                    raise MindModelError(last_error, permanent=True) from exc
            except Exception as exc:
                last_error = str(exc)
                if _is_permanent_error(exc):
                    raise MindModelError(last_error, permanent=True) from exc

            if attempt < self.config.max_retries:
                time.sleep(min(0.5 * (2 ** attempt), 2.0))

        raise MindModelError(f"心智状态分析重试后失败: {last_error}")

    def close(self) -> None:
        self.client.close()

    def _response_format(self) -> dict | None:
        if self._response_mode == "json_schema":
            return {"type": "json_schema", "json_schema": _JSON_SCHEMA}
        if self._response_mode == "json_object":
            return {"type": "json_object"}
        return None


def _pick(source: dict, keys: tuple[str, ...]) -> dict:
    return {key: source.get(key) for key in keys}


def _compact_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors()[:8]:
            path = ".".join(str(item) for item in error.get("loc", ()))
            parts.append(f"{path}: {error.get('msg', '校验失败')}")
        return "; ".join(parts)
    return str(exc)[:500]


def _is_permanent_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return status in (401, 403, 404)
