"""心智模型状态分析：严格 JSON、带上下文纠错与有限重试。"""

import json
import threading
import time
from pathlib import Path

from openai import BadRequestError, OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import MindConfig
from .context_builder import format_turns
from .model_runtime import MindModelRuntime
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
    def __init__(self, message: str, *, permanent: bool = False,
                 runtime_version: int | None = None):
        super().__init__(message)
        self.permanent = permanent
        self.runtime_version = runtime_version


_JSON_SCHEMA = {
    "name": "fengjin_mind_state",
    "strict": True,
    "schema": StateAnalysisResult.model_json_schema(),
}


class StateAnalyzer:
    def __init__(self, config: MindConfig, client: OpenAI | None = None,
                 model: str = "", *, runtime: MindModelRuntime | None = None):
        self.config = config
        if runtime is None:
            if client is None:
                raise ValueError("StateAnalyzer 需要 client 或 runtime")
            runtime = MindModelRuntime.single_client(client, model)
            self._owns_runtime = True
        else:
            self._owns_runtime = False
        self.runtime = runtime
        self.log = get_logger("mind_state")
        root = Path(__file__).resolve().parent.parent.parent
        prompt_path = Path(config.prompt_file)
        if not prompt_path.is_absolute():
            prompt_path = root / prompt_path
        self.prompt = prompt_path.read_text(encoding="utf-8")
        self._mode_lock = threading.Lock()
        self._response_modes: dict[int, str] = {}

    def analyze(self, turns: list[dict], mood: dict, bond: dict,
                trace_id: str = "") -> StateAnalysisResult:
        with self.runtime.acquire("state") as lease:
            return self._analyze_with_runtime(
                turns, mood, bond, trace_id, lease
            )

    def _analyze_with_runtime(self, turns: list[dict], mood: dict, bond: dict,
                              trace_id: str, lease) -> StateAnalysisResult:
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
        attempt = 0
        response_mode = self._get_response_mode(lease.version)
        while attempt <= self.config.max_retries:
            try:
                kwargs = {
                    "model": lease.model,
                    "messages": messages,
                    "max_tokens": self.config.state_max_tokens,
                    "temperature": 0.1,
                }
                response_format = self._response_format(response_mode)
                if response_format is not None:
                    kwargs["response_format"] = response_format
                response = lease.client.chat.completions.create(**kwargs)
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
                unsupported_markers = (
                    "response_format", "json_schema", "json_object",
                    "json mode", "structured output",
                )
                if response_mode in ("json_schema", "json_object") and any(
                    marker in lowered for marker in unsupported_markers
                ):
                    previous_mode = response_mode
                    response_mode = (
                        "json_object" if previous_mode == "json_schema" else "prompt_only"
                    )
                    self._set_response_mode(lease.version, response_mode)
                    log.warning("供应商不支持 {}，降级为 {}", previous_mode, response_mode)
                    last_error = text
                    # 能力协商不消耗业务重试次数。
                    continue
                last_error = text
                if _is_permanent_error(exc):
                    raise MindModelError(
                        last_error,
                        permanent=True,
                        runtime_version=lease.version,
                    ) from exc
            except Exception as exc:
                last_error = str(exc)
                if _is_permanent_error(exc):
                    raise MindModelError(
                        last_error,
                        permanent=True,
                        runtime_version=lease.version,
                    ) from exc

            if attempt < self.config.max_retries:
                time.sleep(min(0.5 * (2 ** attempt), 2.0))
            attempt += 1

        raise MindModelError(
            f"心智状态分析重试后失败: {last_error}",
            runtime_version=lease.version,
        )

    def close(self) -> None:
        if self._owns_runtime:
            self.runtime.close()

    def _get_response_mode(self, version: int) -> str:
        with self._mode_lock:
            return self._response_modes.setdefault(version, "json_schema")

    def _set_response_mode(self, version: int, mode: str) -> None:
        with self._mode_lock:
            self._response_modes[version] = mode

    @staticmethod
    def _response_format(mode: str) -> dict | None:
        if mode == "json_schema":
            return {"type": "json_schema", "json_schema": _JSON_SCHEMA}
        if mode == "json_object":
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
