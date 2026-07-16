"""心智模型改造的核心回归测试。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.agent.context_manager import estimate_messages_tokens
from src.mind.config import MindConfig
from src.mind.context_builder import normalize_turns
from src.mind.state_analyzer import StateAnalyzer
from src.mood.engine import MoodEngine, MoodSettings
from src.bond.tracker import BondSettings, BondTracker


class _FakeCompletions:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = next(self.outputs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class _FakeClient:
    def __init__(self, outputs):
        self.chat = SimpleNamespace(completions=_FakeCompletions(outputs))

    def close(self):
        pass


class MindContextTests(unittest.TestCase):
    def test_tool_messages_are_removed_and_count_as_one_turn(self):
        messages = [
            {"role": "user", "content": "风堇，小伊卡有什么能力？"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "不应进入心智上下文"},
            {"role": "assistant", "content": "小伊卡能替我照看庭院。"},
            {"role": "user", "content": "我今天很开心。"},
            {"role": "assistant", "content": "听见你这么说，我也很开心。"},
        ]
        turns = normalize_turns(messages, max_turns=3, max_tokens=50_000)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["assistant"], "小伊卡能替我照看庭院。")
        self.assertNotIn("不应进入", json.dumps(turns, ensure_ascii=False))

    def test_token_budget_drops_old_complete_turns(self):
        messages = []
        for i in range(4):
            messages.extend([
                {"role": "user", "content": f"第{i}轮" + "甲" * 40},
                {"role": "assistant", "content": "乙" * 40},
            ])
        turns = normalize_turns(messages, max_turns=3, max_tokens=100)
        self.assertLessEqual(len(turns), 2)
        self.assertIn("第3轮", turns[-1]["user"])
        self.assertLessEqual(
            estimate_messages_tokens([
                {"role": "user", "content": turns[-1]["user"]},
                {"role": "assistant", "content": turns[-1]["assistant"]},
            ]),
            100,
        )


class StateAnalyzerTests(unittest.TestCase):
    def test_schema_failure_keeps_retry_history(self):
        invalid = '{"mood":{"pleasure":0.7}}'
        valid = json.dumps({
            "mood": {"pleasure": 0.7, "arousal": 0.3, "dominance": 0.5},
            "bond": {"warmth": 0.62, "trust": 0.25, "formality": 0.45, "humor": 0.15},
        })
        client = _FakeClient([invalid, valid])
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            prompt.write_text("只输出JSON", encoding="utf-8")
            analyzer = StateAnalyzer(
                MindConfig(prompt_file=str(prompt), max_retries=3), client, "fake"
            )
            result = analyzer.analyze(
                [{"user": "你好", "assistant": "你好，灰宝。"}],
                {"pleasure": 0.65, "arousal": 0.25, "dominance": 0.52},
                {"warmth": 0.62, "trust": 0.25, "formality": 0.45, "humor": 0.15},
            )
        self.assertEqual(result.bond.trust, 0.25)
        second_messages = client.chat.completions.calls[1]["messages"]
        self.assertEqual(second_messages[-2]["role"], "assistant")
        self.assertIn("未通过 JSON Schema", second_messages[-1]["content"])


class StateFreezeTests(unittest.TestCase):
    def test_disabled_state_does_not_decay_or_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            mood = MoodEngine(MoodSettings(), data_dir=data_dir)
            bond = BondTracker(BondSettings(), data_dir=data_dir)
            mood.update(pleasure=0.9)
            bond.update(warmth=0.8)
            mood.set_enabled(False)
            bond.set_enabled(False)
            mood_before = dict(mood.load())
            bond_before = dict(bond.load())
            mood.update(pleasure=-1.0)
            bond.update(warmth=0.0)
            self.assertEqual(mood.load()["pleasure"], mood_before["pleasure"])
            self.assertEqual(bond.load()["warmth"], bond_before["warmth"])

    def test_cleanup_then_reenable_preserves_persisted_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            mood = MoodEngine(MoodSettings(), data_dir=data_dir)
            bond = BondTracker(BondSettings(), data_dir=data_dir)
            mood_value = mood.update(pleasure=0.8)["pleasure"]
            bond_value = bond.update(trust=0.3)["trust"]
            mood.cleanup()
            bond.cleanup()
            mood.set_enabled(True)
            bond.set_enabled(True)
            self.assertAlmostEqual(mood.load()["pleasure"], mood_value, places=4)
            self.assertAlmostEqual(bond.load()["trust"], bond_value, places=4)


if __name__ == "__main__":
    unittest.main()
