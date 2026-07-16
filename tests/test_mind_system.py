"""心智模型改造的核心回归测试。"""

import json
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import BadRequestError

from src.agent.context_manager import estimate_messages_tokens
from src.mind.config import MindConfig
from src.mind.context_builder import normalize_turns
from src.mind.manager import MindManager
from src.mind.state_analyzer import StateAnalysisResult, StateAnalyzer
from src.memory.writer import MemoryWriter
from src.memory.manager import MemoryManager
from src.memory.config import MemoryConfig
from src.server.config_manager import ConfigManager
from src.session import MessageMeta, SessionManager
from src.utils.logger import get_logger
from src.mood.engine import MoodEngine, MoodSettings
from src.bond.tracker import BondSettings, BondTracker


class _FakeCompletions:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = next(self.outputs)
        if isinstance(content, Exception):
            raise content
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class _FakeClient:
    def __init__(self, outputs):
        self.chat = SimpleNamespace(completions=_FakeCompletions(outputs))

    def close(self):
        pass


class MindContextTests(unittest.TestCase):
    def test_mind_history_uses_raw_user_text_instead_of_skill_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SessionManager(data_dir=tmp)
            manager.append_message(
                "user",
                "[内部Skill指令] 请按特殊格式处理\n我今天很开心",
                MessageMeta(raw_content="我今天很开心"),
            )
            manager.append_message("assistant", "听见你开心，我也很高兴。")

            dialogue_history = manager.get_current_messages()
            mind_history = manager.get_current_messages(raw_user_content=True)

            self.assertIn("内部Skill指令", dialogue_history[0]["content"])
            self.assertEqual(mind_history[0]["content"], "我今天很开心")
            self.assertEqual(
                manager.current_session.messages[0].display_content,
                "我今天很开心",
            )
            self.assertEqual(manager.current_session.title, "我今天很开心")

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
    def test_schema_rejects_coerced_and_non_finite_numbers(self):
        invalid_payloads = [
            {
                "mood": {"pleasure": "0.5", "arousal": 0.3, "dominance": 0.5},
                "bond": {"warmth": 0.62, "trust": 0.25, "formality": 0.45, "humor": 0.15},
            },
            {
                "mood": {"pleasure": 0.5, "arousal": True, "dominance": 0.5},
                "bond": {"warmth": 0.62, "trust": 0.25, "formality": 0.45, "humor": 0.15},
            },
            {
                "mood": {"pleasure": 0.5, "arousal": 0.3, "dominance": 0.5},
                "bond": {"warmth": float("nan"), "trust": 0.25, "formality": 0.45, "humor": 0.15},
            },
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    StateAnalysisResult.model_validate(payload)

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

    def test_response_format_falls_back_to_prompt_only(self):
        unsupported = lambda: BadRequestError(
            "response_format is not supported",
            response=httpx.Response(
                400, request=httpx.Request("POST", "https://example.test/chat")
            ),
            body=None,
        )
        valid = json.dumps({
            "mood": {"pleasure": 0.7, "arousal": 0.3, "dominance": 0.5},
            "bond": {"warmth": 0.62, "trust": 0.25, "formality": 0.45, "humor": 0.15},
        })
        client = _FakeClient([unsupported(), unsupported(), valid])
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            prompt.write_text("只输出JSON", encoding="utf-8")
            analyzer = StateAnalyzer(
                MindConfig(prompt_file=str(prompt), max_retries=3), client, "fake"
            )
            analyzer.analyze(
                [{"user": "你好", "assistant": "你好，灰宝。"}],
                {"pleasure": 0.65, "arousal": 0.25, "dominance": 0.52},
                {"warmth": 0.62, "trust": 0.25, "formality": 0.45, "humor": 0.15},
            )
        calls = client.chat.completions.calls
        self.assertEqual(calls[0]["response_format"]["type"], "json_schema")
        self.assertEqual(calls[1]["response_format"]["type"], "json_object")
        self.assertNotIn("response_format", calls[2])


class MindManagerBoundaryTests(unittest.TestCase):
    def test_memory_merge_retries_then_drops_conflicting_fact(self):
        class _TemporaryModelError(RuntimeError):
            status_code = 500

        class _Storage:
            def __init__(self):
                self.added = []
                self.upserted = []

            def query(self, **_kwargs):
                return {"ids": [["old-id"]], "distances": [[0.1]]}

            def get(self, **_kwargs):
                return {
                    "documents": ["用户24岁"],
                    "metadatas": [{"is_core": 0}],
                }

            def add(self, **kwargs):
                self.added.append(kwargs)

            def upsert(self, **kwargs):
                self.upserted.append(kwargs)

        client = _FakeClient([
            _TemporaryModelError("temporary-1"),
            _TemporaryModelError("temporary-2"),
            _TemporaryModelError("temporary-3"),
        ])
        writer = object.__new__(MemoryWriter)
        writer.config = MemoryConfig()
        writer.client = client
        writer.model = "fake"
        writer.storage = _Storage()
        writer.max_retries = 2
        writer.log = get_logger("memory_merge_retry_test")
        writer._merge_prompt_template = "旧：{old_memory}\n新：{new_fact}"
        writer._checkpoint = lambda: None

        with patch("src.memory.writer.time.sleep"):
            writer._process_fact({
                "content": "用户25岁",
                "type": "semantic",
                "importance": "low",
            })

        self.assertEqual(len(client.chat.completions.calls), 3)
        self.assertEqual(writer.storage.added, [])
        self.assertEqual(writer.storage.upserted, [])

    def test_state_worker_reads_latest_state_when_each_task_is_consumed(self):
        class _State:
            def __init__(self, value):
                self.value = dict(value)

            def load(self):
                return dict(self.value)

            def update(self, **targets):
                self.value.update(targets)
                return dict(self.value)

        class _Analyzer:
            def __init__(self):
                self.inputs = []

            def analyze(self, _turns, mood, bond, _trace_id):
                self.inputs.append((dict(mood), dict(bond)))
                next_pleasure = 0.1 if len(self.inputs) == 1 else 0.2
                return StateAnalysisResult.model_validate({
                    "mood": {
                        "pleasure": next_pleasure,
                        "arousal": 0.3,
                        "dominance": 0.4,
                    },
                    "bond": {
                        "warmth": 0.6,
                        "trust": 0.3,
                        "formality": 0.4,
                        "humor": 0.2,
                    },
                })

        manager = object.__new__(MindManager)
        manager._lock = threading.RLock()
        manager._enabled = True
        manager._ready = True
        manager._cleaned = False
        manager._generation = 1
        manager.config = MindConfig()
        manager.max_context_tokens = 1_000
        manager.log = get_logger("mind_state_fifo_test")
        manager.memory_manager = None
        manager.state_analyzer = _Analyzer()
        manager.mood_engine = _State({
            "pleasure": 0.0, "arousal": 0.2, "dominance": 0.3,
        })
        manager.bond_tracker = _State({
            "warmth": 0.5, "trust": 0.2, "formality": 0.5, "humor": 0.1,
        })
        manager._queue = queue.Queue()
        worker = threading.Thread(target=manager._worker_loop, args=(manager._queue,))
        worker.start()

        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，灰宝。"},
        ]
        manager.submit(messages, "first")
        manager.submit(messages, "second")
        manager._queue.put(None)
        worker.join(timeout=1)

        self.assertEqual(manager.state_analyzer.inputs[0][0]["pleasure"], 0.0)
        self.assertEqual(manager.state_analyzer.inputs[1][0]["pleasure"], 0.1)

    def test_submit_internal_failure_never_escapes_to_dialogue(self):
        manager = object.__new__(MindManager)
        manager._lock = threading.RLock()
        manager._enabled = True
        manager._ready = True
        manager._cleaned = False
        manager.config = MindConfig()
        manager.max_context_tokens = 1_000
        manager.log = get_logger("mind_test")
        with patch("src.mind.manager.normalize_turns", side_effect=RuntimeError("boom")):
            manager.submit([], "trace")

    def test_stale_memory_generation_is_rejected_before_storage_access(self):
        class _FailIfUsedStorage:
            def query(self, **_kwargs):
                raise AssertionError("stale task must not access storage")

        writer = object.__new__(MemoryWriter)
        writer.storage = _FailIfUsedStorage()
        writer._process_fact(
            {"content": "旧任务", "type": "semantic", "importance": "low"},
            should_apply=lambda: False,
        )

    def test_memory_conversation_tasks_are_processed_fifo(self):
        processed = []

        class _Extractor:
            def extract_conversation(self, text, trace_id=""):
                if text == "first":
                    time.sleep(0.02)
                return [{"content": text, "type": "semantic", "importance": "low"}]

        class _Writer:
            _running = True

            def write(self, facts, should_apply=None):
                processed.extend(fact["content"] for fact in facts)

        manager = object.__new__(MemoryManager)
        manager.log = get_logger("memory_fifo_test")
        manager.extractor = _Extractor()
        manager.writer = _Writer()
        manager._conversation_queue = queue.Queue()
        manager._conversation_stop = threading.Event()
        manager._conversation_worker = threading.Thread(
            target=manager._conversation_loop, daemon=True
        )
        manager._conversation_worker.start()
        manager.extract_conversation_async("first")
        manager.extract_conversation_async("second")
        manager._conversation_queue.join()
        manager._conversation_stop.set()
        manager._conversation_queue.put(None)
        manager._conversation_worker.join(timeout=1)

        self.assertEqual(processed, ["first", "second"])


class ConfigHotReloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_config_update_reaches_all_connected_agents(self):
        class _Agent:
            pass

        old_client = object()
        new_client = object()
        old_config = SimpleNamespace(model="old-model")
        new_config = SimpleNamespace(
            api_key="new-key",
            base_url="https://new.test",
            model="new-model",
        )
        first = _Agent()
        second = _Agent()
        first.client = second.client = old_client
        first.config = second.config = old_config
        app = SimpleNamespace(state=SimpleNamespace(
            client=old_client,
            config=old_config,
            mind_manager=None,
        ))
        ConfigManager.register_agent(app, first)
        ConfigManager.register_agent(app, second)
        previous = {
            "FENGJIN_API_KEY": "old-key",
            "FENGJIN_BASE_URL": "https://old.test",
            "FENGJIN_MODEL": "old-model",
            "MIND_ENABLED": "false",
        }
        current = dict(previous, FENGJIN_API_KEY="new-key", FENGJIN_MODEL="new-model")
        with (
            patch.dict(os.environ, current, clear=False),
            patch.object(ConfigManager, "_build_config_from_env", return_value=new_config),
            patch("src.server.config_manager.AsyncOpenAI", return_value=new_client),
        ):
            await ConfigManager.rebuild_clients(
                app,
                {"api_key": "new-key", "base_url": None, "model": "new-model"},
                {"api_key": None, "base_url": None, "model": None},
                False,
                previous_environ=previous,
            )

        self.assertIs(app.state.client, new_client)
        self.assertIs(app.state.config, new_config)
        for agent in (first, second):
            self.assertIs(agent.client, new_client)
            self.assertIs(agent.config, new_config)

    async def test_changed_mind_config_rebuilds_off_event_loop_thread(self):
        event_loop_thread = threading.get_ident()

        class _Mind:
            memory_manager = object()

            def __init__(self):
                self.thread_id = None

            def reconfigure(self, _enabled):
                self.thread_id = threading.get_ident()

        mind = _Mind()
        app = SimpleNamespace(state=SimpleNamespace(mind_manager=mind))
        previous = {
            "FENGJIN_API_KEY": "main-key",
            "FENGJIN_BASE_URL": "https://main.test",
            "FENGJIN_MODEL": "main-model",
            "MIND_API_KEY": "old-mind-key",
            "MIND_BASE_URL": "https://mind.test",
            "MIND_MODEL": "mind-model",
            "MIND_ENABLED": "true",
        }
        current = dict(previous, MIND_API_KEY="new-mind-key")
        with patch.dict(os.environ, current, clear=False):
            await ConfigManager.rebuild_clients(
                app,
                {"api_key": None, "base_url": None, "model": None},
                {"api_key": "new-mind-key", "base_url": None, "model": None},
                True,
                previous_environ=previous,
            )

        self.assertIsNotNone(mind.thread_id)
        self.assertNotEqual(mind.thread_id, event_loop_thread)

    async def test_unchanged_mind_config_does_not_restart_worker(self):
        class _Mind:
            memory_manager = object()

            def __init__(self):
                self.calls = 0

            def reconfigure(self, _enabled):
                self.calls += 1

        mind = _Mind()
        app = SimpleNamespace(state=SimpleNamespace(mind_manager=mind))
        previous = {
            "FENGJIN_API_KEY": "main-key",
            "FENGJIN_BASE_URL": "https://main.test",
            "FENGJIN_MODEL": "main-model",
            "MIND_API_KEY": "mind-key",
            "MIND_BASE_URL": "https://mind.test",
            "MIND_MODEL": "mind-model",
            "MIND_ENABLED": "true",
        }
        with patch.dict(os.environ, previous, clear=False):
            await ConfigManager.rebuild_clients(
                app,
                {"api_key": None, "base_url": None, "model": None},
                {"api_key": None, "base_url": None, "model": None},
                True,
                previous_environ=previous,
            )
        self.assertEqual(mind.calls, 0)


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

    def test_disabled_state_stays_frozen_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            mood = MoodEngine(MoodSettings(), data_dir=data_dir)
            bond = BondTracker(BondSettings(), data_dir=data_dir)
            mood.update(pleasure=0.95)
            bond.update(warmth=0.9)
            mood.set_enabled(False)
            bond.set_enabled(False)
            mood_value = mood.load()["pleasure"]
            bond_value = bond.load()["warmth"]

            mood_restarted = MoodEngine(MoodSettings(), data_dir=data_dir)
            bond_restarted = BondTracker(BondSettings(), data_dir=data_dir)
            mood_restarted.set_enabled(False)
            bond_restarted.set_enabled(False)
            self.assertAlmostEqual(mood_restarted.load()["pleasure"], mood_value, places=6)
            self.assertAlmostEqual(bond_restarted.load()["warmth"], bond_value, places=6)

            mood_restarted.set_enabled(True)
            bond_restarted.set_enabled(True)
            self.assertAlmostEqual(mood_restarted.load()["pleasure"], mood_value, places=6)
            self.assertAlmostEqual(bond_restarted.load()["warmth"], bond_value, places=6)


if __name__ == "__main__":
    unittest.main()
