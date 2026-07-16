"""风堇羁绊状态机 — 4 维 + change clamp + 接近度衰减 + 指数衰减 + JSON 持久化

设计要点：
  - 纯 Python，零新依赖。路径以 Path(__file__).resolve() 为基准（红线19）。
  - cleanup() 幂等（_cleaned 标志位），initialize() 重置（红线18）。
  - 所有异常记录 logger.error()，不静默吞（红线8）。
  - 衰减在 load/update 时懒计算，不需后台线程。
  - 架构对齐 MoodEngine，更新层不同：change clamp + 接近度衰减 替代 EMA。
  - 漂移保护含硬刹车（比 mood 更严格——羁绊不应漂移）。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..utils.logger import get_logger

# ── 默认值 ──────────────────────────────────────────────────

_DEFAULT_WARMTH = 0.62
_DEFAULT_TRUST = 0.25
_DEFAULT_FORMALITY = 0.45
_DEFAULT_HUMOR = 0.15
_DEFAULT_CHANGE_CLAMP = 0.05
_DEFAULT_PROXIMITY_FLOOR = 0.25
_DEFAULT_PROXIMITY_POWER = 4
_DEFAULT_CHANGE_WARN = 0.08
_DEFAULT_MIN_INTERVAL_SEC = 36

# ── 衰减默认 ────────────────────────────────────────────────

_DEFAULT_WARMTH_HALF_LIFE = 336.0
_DEFAULT_WARMTH_BASELINE = 0.40
_DEFAULT_TRUST_HALF_LIFE = 4320.0
_DEFAULT_TRUST_BASELINE = 0.25
_DEFAULT_FORMALITY_HALF_LIFE = 336.0
_DEFAULT_FORMALITY_BASELINE = 0.65
_DEFAULT_HUMOR_HALF_LIFE = 672.0
_DEFAULT_HUMOR_BASELINE = 0.20

# ── 漂移保护默认 ────────────────────────────────────────────

_DEFAULT_SESSION_CUMULATIVE_WARN = 0.25
_DEFAULT_SESSION_CUMULATIVE_BRAKE = 0.35
_DEFAULT_CONSECUTIVE_SAME_WARN = 8
_DEFAULT_CONSECUTIVE_SAME_BRAKE = 12

# ── 标签默认 ────────────────────────────────────────────────

_DEFAULT_STAGE_CLOSE = 0.55
_DEFAULT_STAGE_FAMILIAR = 0.30

# ── 持久化 ──────────────────────────────────────────────────

_STATE_FILE = "bond_state.json"


@dataclass
class BondSettings:
    """羁绊状态机参数（从 config/bond.yaml 加载）"""

    # 初始值
    default_warmth: float = _DEFAULT_WARMTH
    default_trust: float = _DEFAULT_TRUST
    default_formality: float = _DEFAULT_FORMALITY
    default_humor: float = _DEFAULT_HUMOR

    # 更新控制
    change_clamp: float = _DEFAULT_CHANGE_CLAMP
    proximity_floor: float = _DEFAULT_PROXIMITY_FLOOR
    proximity_power: int = _DEFAULT_PROXIMITY_POWER
    change_warn_threshold: float = _DEFAULT_CHANGE_WARN

    # 衰减
    warmth_half_life_h: float = _DEFAULT_WARMTH_HALF_LIFE
    warmth_baseline: float = _DEFAULT_WARMTH_BASELINE
    trust_half_life_h: float = _DEFAULT_TRUST_HALF_LIFE
    trust_baseline: float = _DEFAULT_TRUST_BASELINE
    formality_half_life_h: float = _DEFAULT_FORMALITY_HALF_LIFE
    formality_baseline: float = _DEFAULT_FORMALITY_BASELINE
    humor_half_life_h: float = _DEFAULT_HUMOR_HALF_LIFE
    humor_baseline: float = _DEFAULT_HUMOR_BASELINE
    min_interval_seconds: float = _DEFAULT_MIN_INTERVAL_SEC

    # 漂移保护
    session_cumulative_warn: float = _DEFAULT_SESSION_CUMULATIVE_WARN
    session_cumulative_brake: float = _DEFAULT_SESSION_CUMULATIVE_BRAKE
    consecutive_same_warn: int = _DEFAULT_CONSECUTIVE_SAME_WARN
    consecutive_same_brake: int = _DEFAULT_CONSECUTIVE_SAME_BRAKE

    # 标签
    stage_close: float = _DEFAULT_STAGE_CLOSE
    stage_familiar: float = _DEFAULT_STAGE_FAMILIAR

    @classmethod
    def load(cls, config_path: str = "config/bond.yaml") -> "BondSettings":
        """从 bond.yaml 加载配置，文件缺失或格式错误回退到默认值。"""
        _root = Path(__file__).resolve().parent.parent.parent
        path = _root / config_path
        if not path.exists():
            return cls()
        try:
            import yaml
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            get_logger("bond").warning("bond.yaml 加载失败，回退默认值: {}", e)
            return cls()
        if not data:
            return cls()

        bond = data.get("bond", {})

        defaults = bond.get("defaults", {})
        update = bond.get("update", {})
        proximity = update.get("proximity", {})
        drift = bond.get("drift_guard", {})
        decay = bond.get("decay", {})
        labels = bond.get("labels", {})

        return cls(
            default_warmth=float(defaults.get("warmth", _DEFAULT_WARMTH)),
            default_trust=float(defaults.get("trust", _DEFAULT_TRUST)),
            default_formality=float(defaults.get("formality", _DEFAULT_FORMALITY)),
            default_humor=float(defaults.get("humor", _DEFAULT_HUMOR)),
            change_clamp=float(update.get("change_clamp", _DEFAULT_CHANGE_CLAMP)),
            proximity_floor=float(proximity.get("floor", _DEFAULT_PROXIMITY_FLOOR)),
            proximity_power=int(proximity.get("power", _DEFAULT_PROXIMITY_POWER)),
            change_warn_threshold=float(update.get("change_warn_threshold", _DEFAULT_CHANGE_WARN)),
            warmth_half_life_h=float(decay.get("warmth_half_life_h", _DEFAULT_WARMTH_HALF_LIFE)),
            warmth_baseline=float(decay.get("warmth_baseline", _DEFAULT_WARMTH_BASELINE)),
            trust_half_life_h=float(decay.get("trust_half_life_h", _DEFAULT_TRUST_HALF_LIFE)),
            trust_baseline=float(decay.get("trust_baseline", _DEFAULT_TRUST_BASELINE)),
            formality_half_life_h=float(decay.get("formality_half_life_h", _DEFAULT_FORMALITY_HALF_LIFE)),
            formality_baseline=float(decay.get("formality_baseline", _DEFAULT_FORMALITY_BASELINE)),
            humor_half_life_h=float(decay.get("humor_half_life_h", _DEFAULT_HUMOR_HALF_LIFE)),
            humor_baseline=float(decay.get("humor_baseline", _DEFAULT_HUMOR_BASELINE)),
            min_interval_seconds=float(decay.get("min_interval_seconds", _DEFAULT_MIN_INTERVAL_SEC)),
            session_cumulative_warn=float(drift.get("session_cumulative_warn", _DEFAULT_SESSION_CUMULATIVE_WARN)),
            session_cumulative_brake=float(drift.get("session_cumulative_brake", _DEFAULT_SESSION_CUMULATIVE_BRAKE)),
            consecutive_same_warn=int(drift.get("consecutive_same_warn", _DEFAULT_CONSECUTIVE_SAME_WARN)),
            consecutive_same_brake=int(drift.get("consecutive_same_brake", _DEFAULT_CONSECUTIVE_SAME_BRAKE)),
            stage_close=float(labels.get("stage_close", _DEFAULT_STAGE_CLOSE)),
            stage_familiar=float(labels.get("stage_familiar", _DEFAULT_STAGE_FAMILIAR)),
        )


class BondTracker:
    """风堇羁绊状态机

    维护 4 维羁绊状态（Warmth/Trust/Formality/Humor），提供：
      - load(): 加载状态（含自动衰减）
      - update(w, t, f, h): change clamp + 接近度衰减 更新
      - describe(): 生成注入文本（数字+标签）
      - inject(user_input): 将羁绊状态注入到 user message 头部
      - cleanup(): 幂等清理

    线程安全：单线程使用（Agent.chat() 是单会话串行的）。
    """

    # 维度 key → (baseline, lambda) 映射
    _DIM_MAP = ("warmth", "trust", "formality", "humor")

    def __init__(self, settings: Optional[BondSettings] = None, *, data_dir: Optional[Path] = None):
        self._settings = settings or BondSettings()
        s = self._settings

        # 衰减常数（每维度独立 λ = ln(2) / half_life）
        self._decay_params: dict[str, tuple[float, float]] = {
            "warmth": (s.warmth_baseline, math.log(2) / s.warmth_half_life_h),
            "trust": (s.trust_baseline, math.log(2) / s.trust_half_life_h),
            "formality": (s.formality_baseline, math.log(2) / s.formality_half_life_h),
            "humor": (s.humor_baseline, math.log(2) / s.humor_half_life_h),
        }

        # 持久化路径（红线19：以本文件为基准）
        if data_dir is None:
            _root = Path(__file__).resolve().parent.parent.parent
            data_dir = _root / "data"
        self._state_path = data_dir / _STATE_FILE

        # 运行时状态
        self._state: dict = {}
        self._total_rounds: int = 0
        self._cleaned = False
        self._enabled = True

        # 漂移保护（会话级计数，cleanup 时清零）
        self._session_cumulative: dict[str, float] = {}
        self._consecutive_same: dict[str, int] = {}
        self._last_sign: dict[str, int] = {}
        self._warned_cumulative: set[str] = set()
        self._warned_consecutive: set[str] = set()
        self._session_braked: set[str] = set()

        self.log = get_logger("bond")

    # ── 公开 API ────────────────────────────────────────────

    def load(self) -> dict:
        """加载状态（先衰减，再返回）。首次调用或文件缺失时使用默认值。

        同一进程内 _state 缓存始终是最新的（update() 每次都写盘），
        因此已加载时跳过磁盘读取，避免 inject()+update() 双重复衰减。
        """
        if self._cleaned:
            self.log.warning("load() 在 cleanup() 后调用，将重新加载")
            self._cleaned = False
        if not self._state:
            self._state = self._read_file() or self._default_state()
            self._total_rounds = self._state.get("total_rounds", 0)
        self._decay()
        return self._state

    def update(self, warmth: Optional[float] = None,
               trust: Optional[float] = None,
               formality: Optional[float] = None,
               humor: Optional[float] = None) -> dict:
        """change clamp + 接近度衰减 更新羁绊状态，持久化到磁盘。

        传入 None 的维度保持不变。
        """
        if not self._enabled:
            return self.load()
        s = self._settings
        cur = self.load()  # 确保最新 + 含衰减

        # 捕获更新前值（漂移保护需要计算本轮变化量）
        old_vals = {k: cur[k] for k in self._DIM_MAP}

        targets = {
            "warmth": warmth,
            "trust": trust,
            "formality": formality,
            "humor": humor,
        }

        for dim, target in targets.items():
            if target is None:
                continue
            old = cur[dim]

            # 1. 计算隐含变化量
            raw_change = target - old

            # 2. 安全帽：单轮变化封顶 ±clamp
            raw_change = max(-s.change_clamp, min(s.change_clamp, raw_change))

            # 3. 接近度衰减（仅正向变化——升温渐慢，降温不设障碍）
            if raw_change > 0:
                factor = self._proximity_factor(old)
                raw_change *= factor

            # 4. 单轮跳变告警（在 clamp 之前检查原始 target-old）
            original_delta = target - old
            if abs(original_delta) > s.change_warn_threshold:
                self.log.warning(
                    "羁绊跳变告警: {} LLM目标={:.3f} 当前={:.3f} 隐含变化={:+.3f}",
                    dim, target, old, original_delta,
                )

            # 5. 累加并裁剪
            cur[dim] = max(0.0, min(1.0, old + raw_change))

        # ── 漂移保护：会话累计 + 连续同向（告警 + 硬刹车）──
        for dim in self._DIM_MAP:
            change = cur[dim] - old_vals[dim]
            if change == 0:
                continue

            self._session_cumulative.setdefault(dim, 0.0)
            self._session_cumulative[dim] += change

            # 累计告警
            if (abs(self._session_cumulative[dim]) > s.session_cumulative_warn
                    and dim not in self._warned_cumulative):
                self._warned_cumulative.add(dim)
                self.log.warning("会话累计漂移告警: {} 累计={:+.3f}", dim, self._session_cumulative[dim])

            # 累计硬刹车：后续同向 delta 强制降为 ±0.01
            if abs(self._session_cumulative[dim]) > s.session_cumulative_brake:
                # 回退本轮超出部分（从已更新的 cur[dim] 中扣除）
                sign = 1 if change > 0 else -1
                if (sign > 0 and self._session_cumulative[dim] > s.session_cumulative_brake) or \
                   (sign < 0 and self._session_cumulative[dim] < -s.session_cumulative_brake):
                    # 将本轮 delta 降为 ±0.01
                    capped_delta = 0.01 if sign > 0 else -0.01
                    correction = change - capped_delta
                    cur[dim] = max(0.0, min(1.0, cur[dim] - correction))
                    self._session_cumulative[dim] -= correction
                    if dim not in self._session_braked:
                        self._session_braked.add(dim)
                        self.log.warning("会话累计硬刹车: {} 累计={:+.3f}，本轮强制降为{:+.2f}",
                                        dim, self._session_cumulative[dim], capped_delta)
                    # 重算 change——累计刹车已修改 cur[dim]，后续连续同向需用实际 delta
                    change = cur[dim] - old_vals[dim]

            # 连续同向追踪
            self._consecutive_same.setdefault(dim, 0)
            sign = 1 if change > 0 else -1
            prev = self._last_sign.get(dim, 0)
            if (sign > 0 and prev > 0) or (sign < 0 and prev < 0):
                self._consecutive_same[dim] += 1
            else:
                self._consecutive_same[dim] = 1
            self._last_sign[dim] = sign

            # 连续同向告警
            if (self._consecutive_same[dim] >= s.consecutive_same_warn
                    and dim not in self._warned_consecutive):
                self._warned_consecutive.add(dim)
                self.log.warning("连续同向告警: {} 连续 {} 轮", dim, self._consecutive_same[dim])

            # 连续同向硬刹车
            if self._consecutive_same[dim] >= s.consecutive_same_brake:
                capped_delta = 0.01 if sign > 0 else -0.01
                correction = change - capped_delta
                cur[dim] = max(0.0, min(1.0, cur[dim] - correction))
                self._session_cumulative[dim] -= correction
                if dim not in self._session_braked:
                    self._session_braked.add(dim)
                    self.log.warning("连续同向硬刹车: {} 连续 {} 轮，本轮强制降为{:+.2f}",
                                    dim, self._consecutive_same[dim], capped_delta)

        self._total_rounds += 1
        cur["updated_at_ts"] = time.time()
        cur["total_rounds"] = self._total_rounds
        self._write_file(cur)
        self._state = cur

        self.log.debug(
            "羁绊更新: W={:.2f} T={:.2f} F={:.2f} H={:.2f} (总轮数={})",
            cur["warmth"], cur["trust"], cur["formality"], cur["humor"],
            self._total_rounds,
        )
        return cur

    def describe(self) -> str:
        """生成注入文本：数字 + 标签。"""
        s = self._state or self._default_state()
        w, t, f, h = s["warmth"], s["trust"], s["formality"], s["humor"]
        label = self._stage_label(w, t, f, h)
        return f"[B W{w:+.2f} T{t:+.2f} F{f:+.2f} H{h:+.2f} {label}]"

    def inject(self, user_input: str) -> str:
        """将羁绊状态注入到 user message 头部。

        格式: [B W+0.65 T+0.42 F+0.35 H+0.22 亲近]\n用户输入

        注意：调用方应在调用本方法之后再调用 mood.inject()——这样 mood 行在上、
        羁绊行在下，与 system_prompt.md 文档顺序一致。
        """
        if not self._enabled:
            return user_input
        self.load()  # 确保状态最新（含衰减），首轮也能读到持久化状态
        return f"{self.describe()}\n{user_input}"

    def get_cli_display(self) -> str:
        """生成 /bond CLI 命令输出文本。"""
        s = self._state or self._default_state()
        w, t, f, h = s["warmth"], s["trust"], s["formality"], s["humor"]
        label = self._stage_label(w, t, f, h)
        composite = self._composite_score(w, t, f, h)
        return (
            f"[B W{w:+.2f} T{t:+.2f} F{f:+.2f} H{h:+.2f} {label}]\n"
            f"综合值: {composite:.2f} | 总轮数: {self._total_rounds}"
        )

    def reset_state(self) -> None:
        """重置会话级漂移保护计数器（会话切换时调用）。不重置持久羁绊状态。"""
        self._session_cumulative = {}
        self._consecutive_same = {}
        self._last_sign = {}
        self._warned_cumulative = set()
        self._warned_consecutive = set()
        self._session_braked = set()

    def set_enabled(self, enabled: bool) -> None:
        """暂停/恢复状态；暂停期间数值和衰减计时均冻结。"""
        if enabled == self._enabled:
            return
        if not enabled:
            self.load()
            self._enabled = False
            return
        if not self._state:
            self._state = self._read_file() or self._default_state()
            self._total_rounds = self._state.get("total_rounds", 0)
        self._cleaned = False
        self._state["updated_at_ts"] = time.time()
        self._write_file(self._state)
        self._enabled = True

    def cleanup(self) -> None:
        """幂等清理（红线18）。"""
        if not self._cleaned:
            self._enabled = False
            self._state = {}
            self._total_rounds = 0
            self._session_cumulative = {}
            self._consecutive_same = {}
            self._last_sign = {}
            self._warned_cumulative = set()
            self._warned_consecutive = set()
            self._session_braked = set()
            self._cleaned = True

    # ── 私有 ────────────────────────────────────────────────

    def _default_state(self) -> dict:
        s = self._settings
        return {
            "warmth": s.default_warmth,
            "trust": s.default_trust,
            "formality": s.default_formality,
            "humor": s.default_humor,
            "updated_at_ts": time.time(),
            "total_rounds": 0,
        }

    def _decay(self) -> None:
        """指数衰减——每维度向各自的基线回归。加载/更新时自动调用。"""
        if not self._enabled:
            return
        now = time.time()
        then = self._state.get("updated_at_ts", now)
        hours = (now - then) / 3600
        if hours < self._settings.min_interval_seconds / 3600:
            return

        for dim in self._DIM_MAP:
            baseline, lam = self._decay_params[dim]
            old = self._state[dim]
            decay = math.exp(-lam * hours)
            self._state[dim] = baseline + (old - baseline) * decay

        self._state["updated_at_ts"] = now

        self.log.debug(
            "衰减 applied ({}h): W={:.2f} T={:.2f} F={:.2f} H={:.2f}",
            round(hours, 1),
            self._state["warmth"], self._state["trust"],
            self._state["formality"], self._state["humor"],
        )

    def _proximity_factor(self, old: float) -> float:
        """接近度衰减：四次多项式，正向变化越靠近满分越慢。

        factor = floor + (1 - floor) × (1 - old^power)
        全程 C∞ 光滑，单调递减，满分永远可达（非渐近线）。
        """
        s = self._settings
        return s.proximity_floor + (1 - s.proximity_floor) * (1 - old ** s.proximity_power)

    def _stage_label(self, w: float, t: float, f: float, h: float) -> str:
        """根据 4 维综合值生成阶段标签。"""
        composite = self._composite_score(w, t, f, h)
        s = self._settings
        if composite >= s.stage_close:
            return "亲近"
        elif composite >= s.stage_familiar:
            return "熟悉"
        else:
            return "初识"

    @staticmethod
    def _composite_score(w: float, t: float, f: float, h: float) -> float:
        """综合值：Warmth×0.35 + Trust×0.30 + (1-Formality)×0.20 + Humor×0.15"""
        return w * 0.35 + t * 0.30 + (1 - f) * 0.20 + h * 0.15

    def _read_file(self) -> Optional[dict]:
        if not self._state_path.exists():
            return None
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            required_keys = {"warmth", "trust", "formality", "humor", "updated_at_ts", "total_rounds"}
            if not isinstance(data, dict) or not required_keys.issubset(data.keys()):
                self.log.warning("bond_state.json 结构不完整（缺键），回退默认值")
                return None
            for key in required_keys:
                if not isinstance(data.get(key), (int, float)):
                    self.log.warning("bond_state.json 键 {} 非数字类型，回退默认值", key)
                    return None
            return data
        except Exception as e:
            self.log.warning("bond_state.json 读取失败，回退默认: {}", e)
            return None

    def _write_file(self, state: dict) -> None:
        """原子写入：.tmp → os.replace，防崩溃损坏。"""
        tmp_path = None
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8",
                dir=str(self._state_path.parent), suffix=".tmp", delete=False,
            ) as tf:
                tmp_path = tf.name  # 取在 dump 前——dump 失败时 finally 也能清理
                json.dump(state, tf, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(self._state_path))
        except Exception as e:
            self.log.error("bond_state.json 写入失败: {}", e)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
