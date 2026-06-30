"""安全护栏模块"""

import time

from ..utils.logger import get_logger
from .rule_engine import RuleEngine, SafetyResult, Action
from .guard_model import GuardModel


class SafetyManager:
    """安全模块统一入口，编排规则引擎(P0) + Llama Guard(P1)"""

    def __init__(self, config_path: str = "config/safety.yaml"):
        self.rule_engine = RuleEngine(config_path)
        self.guard_model = GuardModel(config_path)
        self.log = get_logger("safety")

    def check(self, text: str, trace_id: str = "") -> SafetyResult:
        """统一安全检查：P0 规则引擎 → P1 语义检测"""
        log = self.log.bind(trace_id=trace_id) if trace_id else self.log
        t_total_start = time.monotonic()

        # P0: 规则引擎（毫秒级）
        t0 = time.monotonic()
        result = self.rule_engine.check(text, trace_id=trace_id)
        t_p0 = (time.monotonic() - t0) * 1000
        if result.action != Action.PASS:
            log.info("P0 规则引擎拦截 ({:.0f}ms) → {} category={}",
                     t_p0, result.action.value, result.category)
            return result
        log.debug("P0 规则引擎通过 ({:.0f}ms)", t_p0)

        # P1: Llama Guard（语义级；若模型加载失败则跳过）
        if self.guard_model is not None and self.guard_model.enabled:
            t1 = time.monotonic()
            result = self.guard_model.check(text, trace_id=trace_id)
            t_p1 = (time.monotonic() - t1) * 1000
            t_total = (time.monotonic() - t_total_start) * 1000
            if result.action != Action.PASS:
                log.info("P1 Llama Guard 拦截 ({:.0f}ms, 总计 {:.0f}ms) → {} category={}",
                         t_p1, t_total, result.action.value, result.category)
                return result
            log.info("安全检测完成: P0={:.0f}ms P1={:.0f}ms 总计={:.0f}ms → pass",
                     t_p0, t_p1, t_total)
            return SafetyResult()

        t_total = (time.monotonic() - t_total_start) * 1000
        log.info("安全检测完成: P0={:.0f}ms (P1 跳过) 总计={:.0f}ms → pass", t_p0, t_total)
        return SafetyResult()

    def cleanup(self) -> None:
        """释放安全模块资源（P1 模型显存 + P0 规则引擎引用）"""
        if self.guard_model is not None:
            self.guard_model.cleanup()
        self.rule_engine = None  # RuleEngine 无 GPU 资源，仅清理引用保持对称


__all__ = ["RuleEngine", "SafetyManager", "SafetyResult", "Action"]
