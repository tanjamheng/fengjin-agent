"""安全护栏模块"""

from .rule_engine import RuleEngine, SafetyResult, Action
from .guard_model import GuardModel


class SafetyManager:
    """安全模块统一入口，编排规则引擎(P0) + Llama Guard(P1)"""

    def __init__(self, config_path: str = "config/safety.yaml"):
        self.rule_engine = RuleEngine(config_path)
        self.guard_model = GuardModel(config_path)

    def check(self, text: str) -> SafetyResult:
        """统一安全检查：P0 规则引擎 → P1 语义检测"""
        # P0: 规则引擎（毫秒级）
        result = self.rule_engine.check(text)
        if result.action != Action.PASS:
            return result

        # P1: Llama Guard（语义级）
        if self.guard_model.enabled:
            result = self.guard_model.check(text)
            if result.action != Action.PASS:
                return result

        return SafetyResult()


__all__ = ["RuleEngine", "SafetyManager", "SafetyResult", "Action"]
