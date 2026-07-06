"""风堇情绪状态机 — PAD 三维 + EMA 平滑 + 非对称指数衰减 + JSON 持久化

设计要点：
  - 纯 Python，零新依赖。路径以 Path(__file__).resolve() 为基准（红线19）。
  - cleanup() 幂等（_cleaned 标志位），initialize() 重置（红线18）。
  - 所有异常记录 logger.error()，不静默吞（红线8）。
  - 衰减在 load/update 时懒计算，不需后台线程。
  - 非对称半衰期：正向 96h，负向 48h——好心情比坏心情持续更久。
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..utils.logger import get_logger

# ── 标记提取正则 ────────────────────────────────────────────

_MOOD_TAG_RE = re.compile(r"<!--mood:\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*-->")

# ── 默认值 ──────────────────────────────────────────────────

_DEFAULT_PLEASURE = 0.65
_DEFAULT_AROUSAL = 0.25
_DEFAULT_DOMINANCE = 0.52
_DEFAULT_ALPHA = 0.3
_DEFAULT_HALF_LIFE_POSITIVE_H = 96.0
_DEFAULT_HALF_LIFE_NEGATIVE_H = 48.0
_DEFAULT_MIN_INTERVAL_SEC = 36

# ── 阈值默认 ────────────────────────────────────────────────

_DEFAULT_CONSECUTIVE_LOW = 3
_DEFAULT_LOW_PLEASURE_THRESHOLD = -0.2
_DEFAULT_VERY_LOW_PLEASURE_THRESHOLD = -0.4
_DEFAULT_HIGH_AROUSAL_THRESHOLD = 0.7

# ── 标签默认 ────────────────────────────────────────────────

_DEFAULT_PLEASURE_HIGH = 0.4
_DEFAULT_PLEASURE_LOW = -0.4
_DEFAULT_AROUSAL_HIGH = 0.5
_DEFAULT_AROUSAL_LOW = 0.2


@dataclass
class MoodSettings:
    """情绪状态机参数（从 config/mood.yaml 加载）"""

    # PAD 初始值
    default_pleasure: float = _DEFAULT_PLEASURE
    default_arousal: float = _DEFAULT_AROUSAL
    default_dominance: float = _DEFAULT_DOMINANCE

    # EMA
    ema_alpha: float = _DEFAULT_ALPHA

    # 衰减
    half_life_positive_h: float = _DEFAULT_HALF_LIFE_POSITIVE_H
    half_life_negative_h: float = _DEFAULT_HALF_LIFE_NEGATIVE_H
    baseline_pleasure: float = _DEFAULT_PLEASURE
    baseline_arousal: float = _DEFAULT_AROUSAL
    baseline_dominance: float = _DEFAULT_DOMINANCE
    min_interval_seconds: float = _DEFAULT_MIN_INTERVAL_SEC

    # 阈值
    consecutive_low_pleasure: int = _DEFAULT_CONSECUTIVE_LOW
    low_pleasure_threshold: float = _DEFAULT_LOW_PLEASURE_THRESHOLD
    very_low_pleasure_threshold: float = _DEFAULT_VERY_LOW_PLEASURE_THRESHOLD
    high_arousal_threshold: float = _DEFAULT_HIGH_AROUSAL_THRESHOLD

    # 标签
    pleasure_high: float = _DEFAULT_PLEASURE_HIGH
    pleasure_low: float = _DEFAULT_PLEASURE_LOW
    arousal_high: float = _DEFAULT_AROUSAL_HIGH
    arousal_low: float = _DEFAULT_AROUSAL_LOW

    @classmethod
    def load(cls, config_path: str = "config/mood.yaml") -> "MoodSettings":
        """从 mood.yaml 加载配置，文件缺失或格式错误回退到默认值。"""
        # 以本文件为基准计算项目根，确保任何工作目录都能正确加载（红线19）
        _root = Path(__file__).resolve().parent.parent.parent
        path = _root / config_path
        if not path.exists():
            return cls()
        try:
            import yaml
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception:
            return cls()
        if not data:
            return cls()

        mood = data.get("mood", {})

        defaults = mood.get("defaults", {})
        ema = mood.get("ema", {})
        decay = mood.get("decay", {})
        threshold = mood.get("threshold", {})
        labels = mood.get("labels", {})

        return cls(
            default_pleasure=float(defaults.get("pleasure", _DEFAULT_PLEASURE)),
            default_arousal=float(defaults.get("arousal", _DEFAULT_AROUSAL)),
            default_dominance=float(defaults.get("dominance", _DEFAULT_DOMINANCE)),
            ema_alpha=float(ema.get("alpha", _DEFAULT_ALPHA)),
            half_life_positive_h=float(decay.get("half_life_positive_h", _DEFAULT_HALF_LIFE_POSITIVE_H)),
            half_life_negative_h=float(decay.get("half_life_negative_h", _DEFAULT_HALF_LIFE_NEGATIVE_H)),
            baseline_pleasure=float(decay.get("baseline_pleasure", _DEFAULT_PLEASURE)),
            baseline_arousal=float(decay.get("baseline_arousal", _DEFAULT_AROUSAL)),
            baseline_dominance=float(decay.get("baseline_dominance", _DEFAULT_DOMINANCE)),
            min_interval_seconds=float(decay.get("min_interval_seconds", _DEFAULT_MIN_INTERVAL_SEC)),
            consecutive_low_pleasure=int(threshold.get("consecutive_low_pleasure", _DEFAULT_CONSECUTIVE_LOW)),
            low_pleasure_threshold=float(threshold.get("low_pleasure_threshold", _DEFAULT_LOW_PLEASURE_THRESHOLD)),
            very_low_pleasure_threshold=float(threshold.get("very_low_pleasure_threshold", _DEFAULT_VERY_LOW_PLEASURE_THRESHOLD)),
            high_arousal_threshold=float(threshold.get("high_arousal_threshold", _DEFAULT_HIGH_AROUSAL_THRESHOLD)),
            pleasure_high=float(labels.get("pleasure_high", _DEFAULT_PLEASURE_HIGH)),
            pleasure_low=float(labels.get("pleasure_low", _DEFAULT_PLEASURE_LOW)),
            arousal_high=float(labels.get("arousal_high", _DEFAULT_AROUSAL_HIGH)),
            arousal_low=float(labels.get("arousal_low", _DEFAULT_AROUSAL_LOW)),
        )


class MoodEngine:
    """风堇情绪状态机

    维护 PAD 三维情绪状态，提供：
      - load(): 加载状态（含自动衰减）
      - update(p, a, d): EMA 平滑更新
      - describe(): 生成注入文本（数字+标签）
      - check_threshold(): 极端状态检测
      - extract_and_update(full_text): 从 LLM 回复中提取标记并更新（一站式）
      - cleanup(): 幂等清理

    线程安全：单线程使用（Agent.chat() 是单会话串行的）。
    """

    def __init__(self, settings: Optional[MoodSettings] = None, *, data_dir: Optional[Path] = None):
        self._settings = settings or MoodSettings()
        s = self._settings

        # 衰减常数
        self._lam_positive = math.log(2) / s.half_life_positive_h
        self._lam_negative = math.log(2) / s.half_life_negative_h

        # 持久化路径（红线19：以本文件为基准）
        if data_dir is None:
            _root = Path(__file__).resolve().parent.parent.parent
            data_dir = _root / "data"
        self._state_path = data_dir / "mood_state.json"

        # 运行时状态
        self._state: dict = {}
        self._consecutive_low: int = 0
        self._cleaned = False
        self.log = get_logger("mood")

    # ── 公开 API ────────────────────────────────────────────

    def load(self) -> dict:
        """加载状态（先衰减，再返回）。首次调用或文件缺失时使用默认值。"""
        if self._cleaned:
            self.log.warning("load() 在 cleanup() 后调用，将重新加载")
        self._state = self._read_file() or self._default_state()
        self._decay()
        return self._state

    def update(self, pleasure: Optional[float] = None,
               arousal: Optional[float] = None,
               dominance: Optional[float] = None) -> dict:
        """EMA 平滑更新情绪状态，持久化到磁盘。

        传入 None 的维度保持不变。
        """
        s = self._settings
        cur = self.load()  # 确保最新 + 含衰减

        if pleasure is not None:
            cur["pleasure"] = self._ema(cur["pleasure"], pleasure, s.ema_alpha)
        if arousal is not None:
            cur["arousal"] = self._ema(cur["arousal"], arousal, s.ema_alpha)
        if dominance is not None:
            cur["dominance"] = self._ema(cur["dominance"], dominance, s.ema_alpha)

        # clamp
        cur["pleasure"] = max(-1.0, min(1.0, cur["pleasure"]))
        cur["arousal"] = max(0.0, min(1.0, cur["arousal"]))
        cur["dominance"] = max(-1.0, min(1.0, cur["dominance"]))

        # 追踪连续低落
        if cur["pleasure"] < s.low_pleasure_threshold:
            self._consecutive_low += 1
        else:
            self._consecutive_low = 0

        cur["updated_at_ts"] = time.time()
        self._write_file(cur)
        self._state = cur

        self.log.debug(
            "情绪更新: P={:+.2f} A={:.2f} D={:+.2f} (连续低落={})",
            cur["pleasure"], cur["arousal"], cur["dominance"], self._consecutive_low,
        )
        return cur

    def describe(self) -> str:
        """生成注入文本：数字 + 一句话标签。"""
        s = self._state or self._default_state()
        p, a, d = s["pleasure"], s["arousal"], s["dominance"]
        label = self._dominant_label(p, a, d)
        return f"[P{p:+.2f} A{a:.2f} D{d:+.2f} {label}]"

    def check_threshold(self) -> list[str]:
        """检测极端情绪状态，返回需要追加的提醒指令列表。"""
        s = self._state or self._default_state()
        warnings: list[str] = []
        cfg = self._settings

        # 严重低落（单轮即触发）
        if s["pleasure"] < cfg.very_low_pleasure_threshold:
            warnings.append(
                "你今天心情很糟。不要假装没事，温和地回应就好，不需要展开。"
            )
        # 连续低落
        elif (s["pleasure"] < cfg.low_pleasure_threshold
              and self._consecutive_low >= cfg.consecutive_low_pleasure):
            warnings.append(
                "你今天心情持续低落——不要强颜欢笑，但也不要让灰宝担心。"
            )

        # 高唤醒
        if s["arousal"] > cfg.high_arousal_threshold:
            warnings.append(
                "你比平时更健谈了——可以多问灰宝一句，但注意不要过于兴奋。"
            )

        return warnings

    def extract_and_update(self, full_text: str) -> str:
        """从 LLM 回复中提取情绪标记，执行 EMA 更新。

        返回剥离标记后的纯文本（用于存 session 和发前端）。
        LLM 忘记输出标记时跳过更新，静默 log。
        """
        match = _MOOD_TAG_RE.search(full_text)
        if not match:
            self.log.debug("未提取到情绪标记，本轮跳过更新")
            return full_text

        try:
            p = float(match.group(1))
            a = float(match.group(2))
            d = float(match.group(3))
        except (ValueError, IndexError) as e:
            self.log.warning("情绪标记解析失败: {}", e)
            return full_text

        self.update(pleasure=p, arousal=a, dominance=d)

        # 剥离标记——用户永远看不到，不进入会话历史
        clean = _MOOD_TAG_RE.sub("", full_text).rstrip()
        return clean

    def inject(self, user_input: str) -> str:
        """将情绪状态和阈值提醒注入到 user message 开头。

        格式: [P+0.71 A+0.30 D+0.54 平静温暖]\n[提醒] ...\n\n用户输入
        """
        parts = [self.describe()]

        # 阈值提醒拼在情绪行之后
        warnings = self.check_threshold()
        for w in warnings:
            parts.append(f"[提醒] {w}")

        parts.append("")
        parts.append(user_input)
        return "\n".join(parts)

    def cleanup(self) -> None:
        """幂等清理（红线18）。"""
        if not self._cleaned:
            self._state = {}
            self._consecutive_low = 0
            self._cleaned = True

    # ── 私有 ────────────────────────────────────────────────

    def _default_state(self) -> dict:
        s = self._settings
        return {
            "pleasure": s.default_pleasure,
            "arousal": s.default_arousal,
            "dominance": s.default_dominance,
            "updated_at_ts": time.time(),
        }

    def _decay(self) -> None:
        """指数衰减——向风堇的温暖底色回归。加载/更新时自动调用。"""
        now = time.time()
        then = self._state.get("updated_at_ts", now)
        hours = (now - then) / 3600
        if hours < self._settings.min_interval_seconds / 3600:
            return  # 36s 内不衰减

        s = self._settings

        for key, baseline in (
            ("pleasure", s.baseline_pleasure),
            ("arousal", s.baseline_arousal),
            ("dominance", s.baseline_dominance),
        ):
            old = self._state[key]
            diff = old - baseline

            # 非对称半衰期：正向用长半衰期，负向用短半衰期
            if diff >= 0:
                lam = self._lam_positive
            else:
                lam = self._lam_negative

            decay = math.exp(-lam * hours)
            self._state[key] = baseline + diff * decay

        self._state["updated_at_ts"] = now

        self.log.debug(
            "衰减 applied ({}h): P={:+.2f} A={:.2f} D={:+.2f}",
            round(hours, 1),
            self._state["pleasure"], self._state["arousal"], self._state["dominance"],
        )

    @staticmethod
    def _ema(old: float, new: float, alpha: float) -> float:
        return alpha * new + (1 - alpha) * old

    def _dominant_label(self, p: float, a: float, d: float) -> str:
        """根据 PAD 坐标生成风堇风格的情绪标签。"""
        s = self._settings

        # P → 底色
        if p > s.pleasure_high:
            base = "温暖"
        elif p < s.pleasure_low:
            base = "低落"
        else:
            base = "平静"

        # A → 活力叠加（中等 arousal 不叠加）
        if a > s.arousal_high:
            return f"{base}活跃"
        elif a < s.arousal_low:
            return f"{base}慵懒"
        return base

    def _read_file(self) -> Optional[dict]:
        if not self._state_path.exists():
            return None
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            # 恢复连续低落计数（从状态中推断，保守清零）
            self._consecutive_low = 0
            return data if isinstance(data, dict) else None
        except Exception as e:
            self.log.warning("mood_state.json 读取失败，回退默认: {}", e)
            return None

    def _write_file(self, state: dict) -> None:
        """原子写入：.tmp → os.replace，防崩溃损坏。"""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8",
                dir=str(self._state_path.parent), suffix=".tmp", delete=False,
            ) as tf:
                json.dump(state, tf, ensure_ascii=False, indent=2)
                tmp_path = tf.name
            os.replace(tmp_path, str(self._state_path))
        except OSError as e:
            self.log.error("mood_state.json 写入失败: {}", e)
