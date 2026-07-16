"""心智模型改造的核心回归测试。"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import BadRequestError

from src.agent.context_manager import estimate_messages_tokens
from src.agent.core import _build_api_messages
from src.mind.config import MindConfig, MindSettings
from src.mind.context_builder import normalize_turns
from src.mind.manager import MindManager
from src.mind.model_runtime import MindModelRuntime
from src.mind.state_analyzer import StateAnalysisResult, StateAnalyzer
from src.memory.writer import MemoryWriter
from src.memory.manager import MemoryManager
from src.memory.extractor import MemoryExtractor
from src.memory.config import MemoryConfig
from src.server.config_manager import ConfigManager
from src.ws.connection import _apply_config_update
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


class MindModelRuntimeTests(unittest.TestCase):
    def test_swap_preserves_inflight_snapshot_and_delays_old_close(self):
        class _ClosableClient:
            def __init__(self, name):
                self.name = name
                self.closed = False

            def close(self):
                self.closed = True

        old_memory = _ClosableClient("old-memory")
        old_state = _ClosableClient("old-state")
        new_memory = _ClosableClient("new-memory")
        new_state = _ClosableClient("new-state")
        runtime = MindModelRuntime(old_memory, old_state, "old-model")

        old_lease = runtime.acquire("memory")
        runtime.swap(new_memory, new_state, "new-model")

        self.assertIs(old_lease.client, old_memory)
        self.assertEqual(old_lease.model, "old-model")
        self.assertFalse(old_memory.closed)
        self.assertFalse(old_state.closed)
        with runtime.acquire("state") as new_lease:
            self.assertIs(new_lease.client, new_state)
            self.assertEqual(new_lease.model, "new-model")

        old_lease.release()
        self.assertTrue(old_memory.closed)
        self.assertTrue(old_state.closed)
        runtime.close()
        self.assertTrue(new_memory.closed)
        self.assertTrue(new_state.closed)

    def test_config_barrier_holds_queued_acquire_until_committed_runtime(self):
        old_client = _FakeClient([])
        new_client = _FakeClient([])
        runtime = MindModelRuntime(old_client, old_client, "old-model")
        runtime.pause_new_acquires()
        acquired = []
        finished = threading.Event()

        def _acquire():
            with runtime.acquire("memory") as lease:
                acquired.append((lease.client, lease.model))
            finished.set()

        worker = threading.Thread(target=_acquire)
        worker.start()
        self.assertFalse(finished.wait(timeout=0.05))

        runtime.swap(new_client, new_client, "new-model")
        self.assertFalse(finished.wait(timeout=0.05))
        runtime.resume_new_acquires()

        self.assertTrue(finished.wait(timeout=1))
        worker.join(timeout=1)
        self.assertEqual(acquired, [(new_client, "new-model")])
        runtime.close()

    def test_state_request_keeps_old_runtime_for_all_retries(self):
        entered = threading.Event()
        resume = threading.Event()
        valid = json.dumps({
            "mood": {"pleasure": 0.7, "arousal": 0.3, "dominance": 0.5},
            "bond": {"warmth": 0.62, "trust": 0.25, "formality": 0.45, "humor": 0.15},
        })

        class _BlockingCompletions(_FakeCompletions):
            def create(self, **kwargs):
                if not self.calls:
                    entered.set()
                    resume.wait(timeout=1)
                return super().create(**kwargs)

        old_client = _FakeClient([])
        old_client.chat.completions = _BlockingCompletions([
            '{"mood":{"pleasure":0.7}}', valid,
        ])
        new_client = _FakeClient([valid])
        runtime = MindModelRuntime(old_client, old_client, "old-model")
        analyzer = StateAnalyzer(MindConfig(max_retries=1), runtime=runtime)
        result_holder = []

        worker = threading.Thread(target=lambda: result_holder.append(
            analyzer.analyze(
                [{"user": "你好", "assistant": "你好，灰宝。"}],
                {"pleasure": 0.65, "arousal": 0.25, "dominance": 0.52},
                {"warmth": 0.62, "trust": 0.25, "formality": 0.45, "humor": 0.15},
            )
        ))
        worker.start()
        self.assertTrue(entered.wait(timeout=1))
        runtime.swap(new_client, new_client, "new-model")
        resume.set()
        worker.join(timeout=2)

        self.assertEqual(len(result_holder), 1)
        self.assertEqual(len(old_client.chat.completions.calls), 2)
        self.assertEqual(len(new_client.chat.completions.calls), 0)
        self.assertTrue(all(
            call["model"] == "old-model"
            for call in old_client.chat.completions.calls
        ))
        runtime.close()

    def test_retired_runtime_failure_does_not_disable_current_runtime(self):
        callback_calls = []
        manager = object.__new__(MindManager)
        manager._lock = threading.RLock()
        manager._enabled = True
        manager._ready = True
        manager._cleaned = False
        manager._generation = 1
        manager.mood_engine = SimpleNamespace(set_enabled=lambda _value: None)
        manager.bond_tracker = SimpleNamespace(set_enabled=lambda _value: None)

        manager._handle_model_failure(
            1, lambda: callback_calls.append(True), permanent=True,
            current_runtime=False,
        )

        self.assertTrue(manager._ready)
        self.assertEqual(callback_calls, [])

    def test_failure_warning_is_rate_limited_and_permanent_error_invalidates_generation(self):
        callback_calls = []
        manager = object.__new__(MindManager)
        manager._lock = threading.RLock()
        manager._generation_lock = threading.RLock()
        manager._enabled = True
        manager._ready = True
        manager._cleaned = False
        manager._generation = 3
        manager._next_user_warning_at = 0.0
        manager.config = MindConfig(warning_cooldown_seconds=30)
        manager.log = get_logger("mind_warning_cooldown_test")
        manager.mood_engine = SimpleNamespace(set_enabled=lambda _value: None)
        manager.bond_tracker = SimpleNamespace(set_enabled=lambda _value: None)

        manager._handle_model_failure(3, lambda: callback_calls.append("first"), False)
        manager._handle_model_failure(3, lambda: callback_calls.append("second"), False)
        manager._handle_model_failure(3, lambda: callback_calls.append("third"), True)

        self.assertEqual(callback_calls, ["first"])
        self.assertEqual(manager._generation, 4)
        self.assertFalse(manager._ready)


class MindContextTests(unittest.TestCase):
    def test_skill_prompt_is_current_turn_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SessionManager(data_dir=tmp)
            raw_input = "我今天很开心"
            enhanced_input = "[内部Skill指令] 请按特殊格式处理\n我今天很开心"
            manager.append_message("user", raw_input)

            api_messages = _build_api_messages(
                manager,
                enhanced_input,
                "system prompt",
            )

            self.assertEqual(api_messages[-1]["content"], enhanced_input)
            self.assertEqual(manager.get_current_messages()[0]["content"], raw_input)
            self.assertEqual(manager.get_current_messages(raw_user_content=True)[0]["content"], raw_input)

            manager.append_message("assistant", "听见你开心，我也很高兴。")
            manager.append_message("user", "下一轮")
            next_turn_messages = _build_api_messages(
                manager,
                "下一轮",
                "system prompt",
            )

            self.assertEqual(next_turn_messages[1]["content"], raw_input)
            self.assertNotIn("内部Skill指令", str(next_turn_messages))
            self.assertEqual(manager.current_session.title, raw_input)

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
        first_system_prompt = client.chat.completions.calls[0]["messages"][0]["content"]
        self.assertIn("以下是必须严格遵守的 JSON Schema", first_system_prompt)
        self.assertIn('"additionalProperties":false', first_system_prompt)
        self.assertIn('"pleasure"', first_system_prompt)
        self.assertIn('"humor"', first_system_prompt)
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


class MemoryExtractorTests(unittest.TestCase):
    def test_json_object_falls_back_to_prompt_only(self):
        unsupported = BadRequestError(
            "response_format is not supported",
            response=httpx.Response(
                400, request=httpx.Request("POST", "https://example.test/chat")
            ),
            body=None,
        )
        client = _FakeClient([
            unsupported,
            '{"facts":[{"content":"用户喜欢晴天","evidence":"我喜欢晴天","type":"semantic","importance":"low"}]}',
        ])

        class _Storage:
            def query(self, **_kwargs):
                return {"distances": [[]]}

        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "memory.md"
            prompt.write_text("只输出JSON", encoding="utf-8")
            config = MemoryConfig(extraction={"prompt_file": str(prompt)})
            extractor = MemoryExtractor(
                config, client, "fake", _Storage(), max_retries=2
            )
            facts = extractor.extract_conversation("用户：我喜欢晴天")

        self.assertEqual(facts[0]["content"], "用户喜欢晴天")
        self.assertIn("response_format", client.chat.completions.calls[0])
        self.assertNotIn("response_format", client.chat.completions.calls[1])

    def test_non_string_content_enters_json_correction_retry(self):
        client = _FakeClient([
            '{"facts":[{"content":123,"evidence":"我喜欢晴天","type":"semantic","importance":"low"}]}',
            '{"facts":[{"content":"用户喜欢晴天","evidence":"我喜欢晴天","type":"semantic","importance":"low"}]}',
        ])

        class _Storage:
            def query(self, **_kwargs):
                return {"distances": [[]]}

        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "memory.md"
            prompt.write_text("只输出JSON", encoding="utf-8")
            config = MemoryConfig(extraction={"prompt_file": str(prompt)})
            extractor = MemoryExtractor(
                config, client, "fake", _Storage(), max_retries=1
            )
            facts = extractor.extract_conversation("用户：我喜欢晴天")

        self.assertEqual(facts[0]["content"], "用户喜欢晴天")
        retry_messages = client.chat.completions.calls[1]["messages"]
        self.assertIn("fact.content必须是字符串", retry_messages[-1]["content"])

    def test_assistant_only_memory_is_rejected_by_user_evidence_check(self):
        client = _FakeClient([
            '{"facts":[{"content":"灰宝追过银色光尘","evidence":"追着银色光尘跑过","type":"episodic","importance":"low"}]}',
            '{"facts":[]}',
        ])

        class _Storage:
            def query(self, **_kwargs):
                return {"distances": [[]]}

        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "memory.md"
            prompt.write_text("只输出JSON", encoding="utf-8")
            config = MemoryConfig(extraction={"prompt_file": str(prompt)})
            extractor = MemoryExtractor(
                config, client, "fake", _Storage(), max_retries=1
            )
            facts = extractor.extract_conversation(
                "第1轮（最新一轮，分析主体）\n"
                "用户：你的小伊卡有什么能力？\n"
                "风堇：你以前追着银色光尘跑过。"
            )

        self.assertEqual(facts, [])
        retry_messages = client.chat.completions.calls[1]["messages"]
        self.assertIn("必须逐字来自最新一轮用户原话", retry_messages[-1]["content"])


class ColdImportTests(unittest.TestCase):
    def test_memory_manager_imports_in_fresh_interpreter(self):
        project_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "-c", "import src.memory.manager"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class MainPromptRegressionTests(unittest.TestCase):
    def test_main_prompt_does_not_request_legacy_state_markers(self):
        project_root = Path(__file__).resolve().parent.parent
        prompt = (project_root / "config" / "system_prompt.md").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("<!--mood:", prompt)
        self.assertNotIn("<!--bond:", prompt)
        self.assertNotIn("每轮输出格式（必须执行）", prompt)


class MindManagerBoundaryTests(unittest.TestCase):
    def test_background_enable_returns_before_service_startup_finishes(self):
        entered = threading.Event()
        release = threading.Event()
        manager = object.__new__(MindManager)
        manager._lock = threading.RLock()
        manager.log = get_logger("mind_background_start_test")

        def _start_services(_generation, _on_failure=None):
            entered.set()
            release.wait(timeout=1)

        with patch.object(manager, "_prepare_reconfigure", return_value=9), \
             patch.object(manager, "_start_services", side_effect=_start_services):
            started = time.monotonic()
            manager.reconfigure_background(True)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.2)
            self.assertTrue(entered.wait(timeout=1))
            release.set()
            manager._startup_thread.join(timeout=1)
            self.assertFalse(manager._startup_threads)

    def test_stale_startup_stops_waiting_for_retired_memory(self):
        manager = object.__new__(MindManager)
        manager._lock = threading.RLock()
        manager._retired_memory_cleanups = {threading.Event()}

        with patch.object(manager, "_startup_current", return_value=False):
            started = time.monotonic()
            completed = manager._wait_retired_memory_cleanups(generation=9)

        self.assertFalse(completed)
        self.assertLess(time.monotonic() - started, 0.3)

    def test_cleanup_join_ignores_registered_but_unstarted_thread(self):
        manager = object.__new__(MindManager)
        manager._lock = threading.RLock()
        manager.config = SimpleNamespace(cleanup_timeout_seconds=0.1)
        manager.log = get_logger("mind_unstarted_join_test")
        unstarted = threading.Thread(target=lambda: None)
        manager._startup_threads = {unstarted}

        manager._join_startup_threads()

        self.assertIsNone(unstarted.ident)

    def test_background_thread_start_failure_degrades_without_escaping(self):
        manager = object.__new__(MindManager)
        manager._lock = threading.RLock()
        manager._startup_threads = set()
        manager.log = get_logger("mind_start_failure_test")
        warning = lambda: None

        with patch.object(manager, "_prepare_reconfigure", return_value=7), \
             patch.object(manager, "_notify_user_warning") as notify, \
             patch.object(threading.Thread, "start", side_effect=RuntimeError("boom")):
            manager.reconfigure_background(True, warning)

        self.assertFalse(manager._startup_threads)
        notify.assert_called_once_with(warning)

    def test_close_during_background_start_prevents_stale_publish(self):
        constructor_entered = threading.Event()
        release_constructor = threading.Event()
        created = {}

        class _Client:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _Memory:
            def __init__(self, *_args, **_kwargs):
                self.cleaned = False
                created["memory"] = self
                constructor_entered.set()
                release_constructor.wait(timeout=1)

            def cleanup(self):
                self.cleaned = True

            def wait_cleanup(self):
                pass

        class _Analyzer:
            def __init__(self, *_args, **_kwargs):
                self.closed = False
                created["analyzer"] = self

            def close(self):
                self.closed = True

        manager = object.__new__(MindManager)
        manager._lock = threading.RLock()
        manager._generation = 1
        manager._enabled = True
        manager._ready = False
        manager._cleaned = False
        manager._retired_memory_cleanups = set()
        manager.memory_manager = None
        manager.state_analyzer = None
        manager.model_runtime = None
        manager.config = MindConfig()
        manager.memory_config = MemoryConfig()
        manager.log = get_logger("mind_stale_start_test")
        manager.mood_engine = SimpleNamespace(set_enabled=lambda _value: None)
        manager.bond_tracker = SimpleNamespace(set_enabled=lambda _value: None)
        clients = [_Client(), _Client()]

        with patch.object(MindSettings, "validate_environment"), \
             patch.object(MindSettings, "model_name", return_value="model"), \
             patch.object(MindSettings, "create_client", side_effect=clients), \
             patch("src.mind.manager.MemoryManager", _Memory), \
             patch("src.mind.manager.StateAnalyzer", _Analyzer):
            worker = threading.Thread(target=manager._start_services, args=(1,))
            worker.start()
            self.assertTrue(constructor_entered.wait(timeout=1))
            with manager._lock:
                manager._generation = 2
                manager._enabled = False
            release_constructor.set()
            worker.join(timeout=2)

        self.assertIsNone(manager.memory_manager)
        self.assertIsNone(manager.model_runtime)
        self.assertTrue(created["memory"].cleaned)
        self.assertTrue(created["analyzer"].closed)
        self.assertTrue(all(client.closed for client in clients))

    def test_memory_wal_is_persisted_before_task_is_queued(self):
        fact = {"content": "用户喜欢晴天", "type": "semantic", "importance": "low"}
        with tempfile.TemporaryDirectory() as tmp:
            writer = object.__new__(MemoryWriter)
            writer._dump_path = Path(tmp) / "pending_facts.json"
            writer._wal_lock = threading.RLock()
            writer._pending = {}
            writer._callbacks = {}

            class _InspectQueue:
                def put(_self, task_id):
                    payload = json.loads(writer._dump_path.read_text(encoding="utf-8"))
                    persisted_ids = {item["task_id"] for item in payload["tasks"]}
                    self.assertIn(task_id, persisted_ids)

            writer._queue = _InspectQueue()
            writer.write([fact])

    def test_memory_wal_replay_keeps_file_and_skips_bad_items(self):
        valid = {"content": "用户喜欢晴天", "type": "semantic", "importance": "low"}
        with tempfile.TemporaryDirectory() as tmp:
            writer = object.__new__(MemoryWriter)
            writer._dump_path = Path(tmp) / "pending_facts.json"
            writer._dump_path.write_text(
                json.dumps([None, valid], ensure_ascii=False), encoding="utf-8"
            )
            writer._wal_lock = threading.RLock()
            writer._pending = {}
            writer._callbacks = {}
            writer._queue = queue.Queue()
            writer.log = get_logger("memory_wal_replay_test")

            writer._replay_pending()

            self.assertTrue(writer._dump_path.exists())
            self.assertEqual(len(writer._pending), 1)
            task_id = writer._queue.get_nowait()
            self.assertIn(task_id, writer._pending)

    def test_core_file_stage_is_checkpointed_before_refresh(self):
        fact = {"content": "用户生日是今天", "type": "episodic", "importance": "high"}
        with tempfile.TemporaryDirectory() as tmp:
            writer = object.__new__(MemoryWriter)
            writer._dump_path = Path(tmp) / "pending_facts.json"
            writer._wal_lock = threading.RLock()
            record = {"task_id": "task-1", "fact": fact, "stage": "storage"}
            writer._pending = {"task-1": record}
            writer._callbacks = {"task-1": None}
            writer._apply_fact_storage = lambda *_args, **_kwargs: True
            writer._refresh_core_file = lambda: (_ for _ in ()).throw(OSError("disk"))

            with self.assertRaises(OSError):
                writer._process_record(record)

            payload = json.loads(writer._dump_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["tasks"][0]["stage"], "core_file")

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
            def extract_conversation(self, text, trace_id="", runtime_lease=None):
                if text == "first":
                    time.sleep(0.02)
                return [{"content": text, "type": "semantic", "importance": "low"}]

        class _Writer:
            _running = True

            def write(self, facts, should_apply=None):
                processed.extend(fact["content"] for fact in facts)

        manager = object.__new__(MemoryManager)
        manager.log = get_logger("memory_fifo_test")
        manager.model_runtime = MindModelRuntime.single_client(_FakeClient([]), "fake")
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
        manager.model_runtime.close()

        self.assertEqual(processed, ["first", "second"])

    def test_disable_does_not_wait_for_inflight_memory_cleanup(self):
        cleanup_started = threading.Event()
        allow_cleanup = threading.Event()

        class _Memory:
            def cleanup(self):
                cleanup_started.set()
                allow_cleanup.wait(timeout=1)

            def wait_cleanup(self):
                pass

        class _Runtime:
            def close(self):
                pass

        manager = object.__new__(MindManager)
        manager._lock = threading.RLock()
        manager._worker = None
        manager.memory_manager = _Memory()
        manager.state_analyzer = None
        manager.model_runtime = _Runtime()
        manager._queue = queue.Queue()
        manager._retired_memory_cleanups = set()
        manager.log = get_logger("mind_fast_disable_test")

        started = time.monotonic()
        manager._stop_services(wait_for_memory=False)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2)
        self.assertTrue(cleanup_started.wait(timeout=1))
        allow_cleanup.set()

    def test_next_enable_waits_for_retired_memory_cleanup(self):
        cleanup_started = threading.Event()
        allow_cleanup = threading.Event()
        cleanup_complete = threading.Event()

        class _Memory:
            def cleanup(self):
                cleanup_started.set()
                allow_cleanup.wait(timeout=1)
                cleanup_complete.set()

            def wait_cleanup(self):
                cleanup_complete.wait(timeout=1)

        class _Runtime:
            def close(self):
                pass

        manager = object.__new__(MindManager)
        manager._lock = threading.RLock()
        manager._worker = None
        manager.memory_manager = _Memory()
        manager.state_analyzer = None
        manager.model_runtime = _Runtime()
        manager._queue = queue.Queue()
        manager._retired_memory_cleanups = set()
        manager.log = get_logger("mind_reenable_cleanup_test")

        manager._stop_services(wait_for_memory=False)
        self.assertTrue(cleanup_started.wait(timeout=1))

        enable_wait_finished = threading.Event()
        waiter = threading.Thread(
            target=lambda: (
                manager._wait_retired_memory_cleanups(),
                enable_wait_finished.set(),
            )
        )
        waiter.start()
        self.assertFalse(enable_wait_finished.wait(timeout=0.05))

        allow_cleanup.set()
        self.assertTrue(enable_wait_finished.wait(timeout=1))
        waiter.join(timeout=1)

    def test_disable_discards_inflight_state_result(self):
        entered = threading.Event()
        resume = threading.Event()

        class _State:
            def __init__(self, value):
                self.value = dict(value)
                self.update_calls = 0

            def load(self):
                return dict(self.value)

            def update(self, **targets):
                self.update_calls += 1
                self.value.update(targets)
                return dict(self.value)

        class _Analyzer:
            def analyze(self, *_args):
                entered.set()
                resume.wait(timeout=1)
                return StateAnalysisResult.model_validate({
                    "mood": {"pleasure": 0.8, "arousal": 0.3, "dominance": 0.4},
                    "bond": {"warmth": 0.7, "trust": 0.3, "formality": 0.4, "humor": 0.2},
                })

        manager = object.__new__(MindManager)
        manager._lock = threading.RLock()
        manager._enabled = True
        manager._ready = True
        manager._cleaned = False
        manager._generation = 1
        manager.config = MindConfig()
        manager.max_context_tokens = 1_000
        manager.log = get_logger("mind_disable_inflight_test")
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

        manager.submit([
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，灰宝。"},
        ], "trace")
        self.assertTrue(entered.wait(timeout=1))
        with manager._lock:
            manager._enabled = False
            manager._ready = False
            manager._generation += 1
        resume.set()
        manager._queue.put(None)
        worker.join(timeout=2)

        self.assertEqual(manager.mood_engine.update_calls, 0)
        self.assertEqual(manager.bond_tracker.update_calls, 0)


class ConfigHotReloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_enable_is_scheduled_only_after_config_persistence(self):
        order = []

        class _Mind:
            _generation = 1
            model_runtime = None
            memory_manager = None

            def begin_config_update(self):
                return None

            def end_config_update(self, _runtime):
                order.append("barrier_released")

            def reconfigure_background(self, enabled, _on_failure=None):
                order.append(f"reconfigure:{enabled}")

        app = SimpleNamespace(state=SimpleNamespace(
            client=object(),
            config=SimpleNamespace(model="main"),
            mind_manager=_Mind(),
            _active_agents=[],
            _active_chat_count=0,
            _retired_resources=[],
        ))

        def _persist(*_args, **_kwargs):
            order.append("persist")
            return True

        environment = {
            "FENGJIN_API_KEY": "main-key",
            "FENGJIN_BASE_URL": "https://main.test",
            "FENGJIN_MODEL": "main-model",
            "MIND_API_KEY": "mind-key",
            "MIND_BASE_URL": "https://mind.test",
            "MIND_MODEL": "mind-model",
            "MIND_ENABLED": "false",
        }
        with patch.dict(os.environ, environment, clear=False), \
             patch.object(ConfigManager, "apply_to_os_environ"), \
             patch.object(ConfigManager, "update_env_file", side_effect=_persist), \
             patch.object(ConfigManager, "cleanup_retired_resources", new=AsyncMock()):
            success, _errors = await _apply_config_update(
                app,
                {"api_key": None, "base_url": None, "model": None},
                {"api_key": None, "base_url": None, "model": None},
                True,
            )

        self.assertTrue(success)
        self.assertLess(order.index("persist"), order.index("reconfigure:True"))
        self.assertEqual(order[-1], "barrier_released")

    async def test_persist_failure_rolls_runtime_back(self):
        class _Client:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

        old_client = _Client()
        new_client = _Client()
        old_config = SimpleNamespace(model="old")
        new_config = SimpleNamespace(model="new")
        agent = SimpleNamespace(client=old_client, config=old_config)
        app = SimpleNamespace(state=SimpleNamespace(
            client=old_client,
            config=old_config,
            mind_manager=None,
            _active_agents=[agent],
            _active_chat_count=0,
            _retired_resources=[],
        ))

        async def _rebuild(*_args, **_kwargs):
            app.state.client = new_client
            app.state.config = new_config
            agent.client = new_client
            agent.config = new_config
            app.state._retired_resources.append(old_client)

        with patch.object(ConfigManager, "apply_to_os_environ"), \
             patch.object(ConfigManager, "rebuild_clients", new=AsyncMock(side_effect=_rebuild)), \
             patch.object(ConfigManager, "update_env_file", return_value=False):
            success, _errors = await _apply_config_update(
                app,
                {"api_key": "new", "base_url": None, "model": None},
                {"api_key": None, "base_url": None, "model": None},
                False,
            )

        self.assertFalse(success)
        self.assertIs(app.state.client, old_client)
        self.assertIs(agent.client, old_client)
        self.assertNotIn(old_client, app.state._retired_resources)
        self.assertTrue(new_client.closed)

    async def test_main_rebuild_failure_does_not_restart_untouched_mind(self):
        class _Mind:
            _generation = 7
            memory_manager = object()
            model_runtime = None

            def __init__(self):
                self.reconfigure_calls = 0

            def reconfigure_background(self, _enabled, _on_failure=None):
                self.reconfigure_calls += 1

            def begin_config_update(self):
                return None

            def end_config_update(self, _runtime):
                pass

        mind = _Mind()
        old_client = object()
        old_config = SimpleNamespace(model="old")
        app = SimpleNamespace(state=SimpleNamespace(
            client=old_client,
            config=old_config,
            mind_manager=mind,
            _active_agents=[],
            _active_chat_count=0,
            _retired_resources=[],
        ))
        with patch.object(ConfigManager, "apply_to_os_environ"), \
             patch.object(
                 ConfigManager,
                 "rebuild_clients",
                 new=AsyncMock(side_effect=RuntimeError("main failed")),
             ):
            success, _errors = await _apply_config_update(
                app,
                {"api_key": "bad", "base_url": None, "model": None},
                {"api_key": None, "base_url": None, "model": None},
                False,
            )

        self.assertFalse(success)
        self.assertEqual(mind.reconfigure_calls, 0)

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

    async def test_changed_mind_config_swaps_runtime_off_event_loop_thread(self):
        event_loop_thread = threading.get_ident()

        class _Mind:
            memory_manager = object()

            def __init__(self):
                self.thread_id = None
                self.reconfigure_calls = 0

            def reconfigure(self, _enabled):
                self.reconfigure_calls += 1

            def update_model_runtime(self, _on_failure=None):
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
        self.assertEqual(mind.reconfigure_calls, 0)

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


class BondUpdateTests(unittest.TestCase):
    def test_each_bond_dimension_uses_its_own_change_clamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = BondSettings(
                warmth_change_clamp=0.04,
                trust_change_clamp=0.02,
                formality_change_clamp=0.04,
                humor_change_clamp=0.03,
                proximity_floor=1.0,
            )
            bond = BondTracker(settings, data_dir=Path(tmp))
            before = dict(bond.load())
            after = bond.update(
                warmth=1.0,
                trust=1.0,
                formality=0.0,
                humor=1.0,
            )

        self.assertAlmostEqual(after["warmth"] - before["warmth"], 0.04)
        self.assertAlmostEqual(after["trust"] - before["trust"], 0.02)
        self.assertAlmostEqual(after["formality"] - before["formality"], -0.04)
        self.assertAlmostEqual(after["humor"] - before["humor"], 0.03)

    def test_legacy_scalar_change_clamp_remains_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "bond.yaml"
            config_path.write_text(
                "bond:\n  update:\n    change_clamp: 0.01\n",
                encoding="utf-8",
            )
            settings = BondSettings.load(str(config_path))

        self.assertEqual(settings.warmth_change_clamp, 0.01)
        self.assertEqual(settings.trust_change_clamp, 0.01)
        self.assertEqual(settings.formality_change_clamp, 0.01)
        self.assertEqual(settings.humor_change_clamp, 0.01)

    def test_humor_decay_baseline_does_not_exceed_initial_value(self):
        settings = BondSettings()
        self.assertEqual(settings.humor_baseline, settings.default_humor)


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
