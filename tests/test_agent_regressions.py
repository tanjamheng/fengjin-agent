"""针对 RAG 决策、状态标记和模型加载参数的零依赖回归测试。"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent.core import _strip_all_tags
from src.bond.tracker import BondTracker
from src.mcp_servers.rag_server import RAG_RETRIEVE_TOOL
from src.mood.engine import MoodEngine
from src.rag import embedding_registry
from src.rag.strategies.reranker.cross_encoder import CrossEncoderReranker


class _FakeCuda:
    class OutOfMemoryError(RuntimeError):
        pass

    @staticmethod
    def is_available():
        return False

    @staticmethod
    def empty_cache():
        pass


_FAKE_TORCH = types.SimpleNamespace(float16="float16", cuda=_FakeCuda())


class AgentRegressionTests(unittest.TestCase):
    def tearDown(self):
        embedding_registry._model = None
        embedding_registry._model_path = None
        embedding_registry._model_device = None
        embedding_registry._refcount = 0

    def test_rag_tool_contract_requires_game_facts_to_be_retrieved(self):
        description = RAG_RETRIEVE_TOOL["function"]["description"]

        self.assertIn("人物关系", description)
        self.assertIn("必须先调用", description)
        self.assertIn("不得编造", description)

    def test_state_markers_accept_safe_variants_and_are_removed(self):
        with tempfile.TemporaryDirectory() as data_dir:
            mood = MoodEngine(data_dir=Path(data_dir))
            mood.update = Mock()
            bond = BondTracker(data_dir=Path(data_dir))
            bond.update = Mock()

            raw = (
                "早安，灰宝。"
                "<!-- MOOD : +.65, 0.28, .50 -->"
                "<!-- Bond: .63,+0.25,0.44,.15 -->"
            )
            clean = mood.extract_and_update(raw)
            clean = bond.extract_and_update(clean)

            self.assertEqual(clean, "早安，灰宝。")
            mood.update.assert_called_once_with(
                pleasure=0.65, arousal=0.28, dominance=0.50
            )
            bond.update.assert_called_once_with(
                warmth=0.63, trust=0.25, formality=0.44, humor=0.15
            )

    def test_fallback_removes_malformed_state_marker_candidates(self):
        raw = "正文<!-- mood:0.6,错误,0.5 --><!--bond:格式错误-->"

        self.assertEqual(_strip_all_tags(raw), "正文")

    def test_embedding_loader_passes_compatibility_kwargs(self):
        with tempfile.TemporaryDirectory() as model_dir:
            model_path = Path(model_dir)
            (model_path / ".state").write_text("fp16", encoding="utf-8")
            fake_model = types.SimpleNamespace(device="cpu")
            constructor = Mock(return_value=fake_model)
            fake_module = types.SimpleNamespace(SentenceTransformer=constructor)

            with patch.dict(
                sys.modules,
                {"sentence_transformers": fake_module, "torch": _FAKE_TORCH},
            ):
                model, is_shared = embedding_registry.acquire_handle(
                    str(model_path), device="cpu"
                )

            self.assertIs(model, fake_model)
            self.assertTrue(is_shared)
            self.assertEqual(
                constructor.call_args.kwargs["tokenizer_kwargs"],
                {"fix_mistral_regex": False},
            )
            self.assertIn("dtype", constructor.call_args.kwargs["model_kwargs"])

    def test_reranker_loader_passes_compatibility_kwargs(self):
        with tempfile.TemporaryDirectory() as model_dir:
            model_path = Path(model_dir)
            (model_path / ".state").write_text("fp16", encoding="utf-8")
            fake_model = object()
            constructor = Mock(return_value=fake_model)
            fake_module = types.SimpleNamespace(CrossEncoder=constructor)

            reranker = CrossEncoderReranker(model=str(model_path), device="cpu")
            with patch.dict(
                sys.modules,
                {"sentence_transformers": fake_module, "torch": _FAKE_TORCH},
            ):
                reranker.initialize()

            self.assertIs(reranker._model, fake_model)
            self.assertEqual(
                constructor.call_args.kwargs["tokenizer_kwargs"],
                {"fix_mistral_regex": False},
            )
            self.assertIn("dtype", constructor.call_args.kwargs["model_kwargs"])


if __name__ == "__main__":
    unittest.main()
