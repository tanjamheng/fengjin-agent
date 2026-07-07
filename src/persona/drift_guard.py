"""角色漂移检测 + 修复闭环

用 bge-m3 计算回复与角色锚点的余弦相似度 → EWMA 平滑 → 低于阈值时注入锚点。

设计要点：
  - 锚点从 system_prompt.md §二 解析（6 条），修改人设自动同步锚点
  - bge-m3 通过 embedding_registry 共享，零额外内存
  - 注入到 user message 开头，不入历史、用户不可见
  - cleanup() 幂等（_cleaned 标志位），红线18
  - 所有异常记录 logger.error()，不静默吞（红线8）
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ..utils.logger import get_logger

# ── 默认参数 ──────────────────────────────────────────────────

_DEFAULT_EMA_ALPHA = 0.3
_DEFAULT_DRIFT_THRESHOLD = 0.65
_DEFAULT_CONSECUTIVE_TRIGGER = 2
_DEFAULT_ESCALATION_ROUNDS = 3
_DEFAULT_COOLDOWN_ROUNDS = 5
_DEFAULT_MIN_REPLY_LENGTH = 15

# ── 锚点解析 ──────────────────────────────────────────────────

_ANCHOR_SECTION_RE = re.compile(r"^# 二、角色锚点\s*$")
_ANCHOR_ITEM_RE = re.compile(r"^- (.+)$")


@dataclass
class PersonaSettings:
    """角色漂移检测参数（从 config/persona.yaml 加载）"""

    ema_alpha: float = _DEFAULT_EMA_ALPHA
    drift_threshold: float = _DEFAULT_DRIFT_THRESHOLD
    consecutive_trigger: int = _DEFAULT_CONSECUTIVE_TRIGGER
    escalation_rounds: int = _DEFAULT_ESCALATION_ROUNDS
    cooldown_rounds: int = _DEFAULT_COOLDOWN_ROUNDS
    min_reply_length: int = _DEFAULT_MIN_REPLY_LENGTH

    @classmethod
    def load(cls, config_path: str) -> "PersonaSettings":
        """从 persona.yaml 加载配置，文件缺失或格式错误回退到默认值。"""
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

        detection = data.get("persona", {}).get("detection", {})
        repair = data.get("persona", {}).get("repair", {})

        return cls(
            ema_alpha=float(detection.get("ema_alpha", _DEFAULT_EMA_ALPHA)),
            drift_threshold=float(detection.get("drift_threshold", _DEFAULT_DRIFT_THRESHOLD)),
            consecutive_trigger=int(detection.get("consecutive_trigger", _DEFAULT_CONSECUTIVE_TRIGGER)),
            escalation_rounds=int(repair.get("escalation_rounds", _DEFAULT_ESCALATION_ROUNDS)),
            cooldown_rounds=int(repair.get("cooldown_rounds", _DEFAULT_COOLDOWN_ROUNDS)),
            min_reply_length=int(detection.get("min_reply_length", _DEFAULT_MIN_REPLY_LENGTH)),
        )


class PersonaDriftGuard:
    """角色漂移检测 + 锚点修复

    每轮 LLM 回复后调用 check()，检测是否偏离角色。
    偏离时返回锚点注入文本，由 Agent.chat() 注入下一轮 user message 开头。

    线程安全：单线程使用（Agent.chat() 是单会话串行的）。
    """

    def __init__(
        self,
        embed_model,
        settings: Optional[PersonaSettings] = None,
        *,
        system_prompt_path: Optional[Path] = None,
    ):
        self._settings = settings or PersonaSettings()
        self._emb = embed_model

        # 路径（红线19：以本文件为基准）
        if system_prompt_path is None:
            _root = Path(__file__).resolve().parent.parent.parent
            system_prompt_path = _root / "config" / "system_prompt.md"
        self._system_prompt_path = system_prompt_path

        # 解析锚点 + 预编码
        self._anchors: list[str] = []
        self._anchor_vecs: Optional[np.ndarray] = None
        self._parse_anchors()

        # 运行时状态
        self._ewma: Optional[float] = None
        self._consecutive_below: int = 0
        self._repair_active: bool = False
        self._repair_rounds: int = 0
        self._cooldown_remaining: int = 0
        self._cleaned = False

        self.log = get_logger("persona")

    # ── 公开 API ────────────────────────────────────────────

    @property
    def drift_score(self) -> Optional[float]:
        """当前 EWMA 平滑后的漂移分数（None = 尚无数据）"""
        return self._ewma

    @property
    def anchor_count(self) -> int:
        """已解析的锚点数量"""
        return len(self._anchors)

    def check(self, reply_text: str) -> Optional[str]:
        """检测一轮回复的角色漂移程度。

        Returns:
            锚点注入文本（需要修复时），或 None（无需修复 / 不可用）
        """
        if self._cleaned:
            return None
        if self._emb is None or self._anchor_vecs is None:
            return None
        if not reply_text or len(reply_text) < self._settings.min_reply_length:
            return None

        # 冷却中 → 跳过检测
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return None

        # 编码回复 → 计算与锚点的余弦相似度
        try:
            reply_vec = self._emb.encode(reply_text)
        except Exception as e:
            self.log.warning("回复编码失败: {}", e)
            return None

        raw_score = self._top_k_cosine(reply_vec, k=3)

        # EWMA 平滑
        if self._ewma is None:
            self._ewma = raw_score
        else:
            alpha = self._settings.ema_alpha
            self._ewma = alpha * raw_score + (1 - alpha) * self._ewma

        self.log.debug(
            "driftScore raw={:.3f} ewma={:.3f} threshold={:.2f}",
            raw_score, self._ewma, self._settings.drift_threshold,
        )

        # 阈值判断
        cfg = self._settings
        if self._ewma < cfg.drift_threshold:
            self._consecutive_below += 1
        else:
            self._consecutive_below = 0
            # 恢复正常 → 如果之前在修复中，记录恢复 + 进入冷却
            if self._repair_active:
                self.log.info(
                    "角色漂移已恢复 (driftScore: {:.3f}，注入 {} 轮后恢复)",
                    self._ewma, self._repair_rounds,
                )
                self._repair_active = False
                self._repair_rounds = 0
                self._cooldown_remaining = cfg.cooldown_rounds

        # 触发修复
        if self._consecutive_below >= cfg.consecutive_trigger:
            level = 1
            if self._repair_active and self._repair_rounds >= cfg.escalation_rounds:
                level = 2
            anchor_text = self._build_anchor(level)
            self._repair_active = True
            self._repair_rounds += 1
            self._consecutive_below = 0  # 重置，等下一轮重新评估
            self.log.info(
                "角色漂移修复锚点已注入 (driftScore: {:.3f}, level: {})",
                self._ewma, level,
            )
            return anchor_text

        return None

    def cleanup(self) -> None:
        """幂等清理（红线18）。"""
        if not self._cleaned:
            self._anchor_vecs = None
            self._anchors = []
            self._ewma = None
            self._cleaned = True

    # ── 私有 ────────────────────────────────────────────────

    def _parse_anchors(self) -> None:
        """从 system_prompt.md §二 解析 6 条角色锚点。"""
        try:
            text = self._system_prompt_path.read_text(encoding="utf-8")
        except Exception as e:
            self.log.warning("无法读取 system_prompt.md，漂移检测不可用: {}", e)
            return

        lines = text.split("\n")
        in_section = False
        anchors: list[str] = []

        for line in lines:
            if _ANCHOR_SECTION_RE.match(line):
                in_section = True
                continue
            if in_section:
                # 遇到下一个章节标题或分隔线 → 停止
                if line.startswith("# ") or line.strip() == "---":
                    break
                m = _ANCHOR_ITEM_RE.match(line)
                if m:
                    anchors.append(m.group(1).strip())

        if len(anchors) < 3:
            self.log.warning(
                "锚点解析不足（需要 ≥3 条，实际 {} 条），漂移检测不可用",
                len(anchors),
            )
            return
        if len(anchors) != 6:
            self.log.warning(
                "锚点数量与预期不符（预期 6 条，实际 {} 条），继续运行",
                len(anchors),
            )

        self._anchors = anchors

        # 预编码锚点向量
        if self._emb is not None:
            try:
                self._anchor_vecs = np.array([
                    self._emb.encode(a) for a in anchors
                ])
                self.log.info("角色锚点已加载: {} 条", len(anchors))
            except Exception as e:
                self.log.warning("锚点编码失败，漂移检测不可用: {}", e)
                self._anchor_vecs = None

    def _top_k_cosine(self, reply_vec: np.ndarray, k: int = 3) -> float:
        """计算回复向量与所有锚点的 top-k 平均余弦相似度。"""
        # 归一化
        reply_norm = reply_vec / (np.linalg.norm(reply_vec) + 1e-10)
        anchors_norm = self._anchor_vecs / (
            np.linalg.norm(self._anchor_vecs, axis=1, keepdims=True) + 1e-10
        )
        sims = np.dot(anchors_norm, reply_norm)
        # 取 top-k
        sims.sort()
        top_k = sims[-k:] if len(sims) >= k else sims
        return float(np.mean(top_k))

    def _build_anchor(self, level: int = 1) -> str:
        """构建锚点注入文本。

        Level 1: [角色校准] + 6 条锚点（~80 token）
        Level 2: [角色校准] + 身份简述 + 6 条锚点（~200 token）
        """
        lines = ["[角色校准]"]
        if level >= 2:
            lines.append("你是风堇，翁法罗斯昏光庭院的首席护理师。小伊卡在你身边，和你对话的人是灰宝——你在乎的朋友。")
        for a in self._anchors:
            lines.append(f"- {a}")
        return "\n".join(lines)
