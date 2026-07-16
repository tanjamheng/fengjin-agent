"""RAG 降级语义与来源元数据测试。"""

from types import SimpleNamespace

from src.agent.context_manager import ContextManager
from src.agent.core import _extract_rag_sources
from src.mcp_servers.rag_server import RAGMCPServer
from src.persona.drift_guard import PersonaSettings


class _EmptyRAG:
    def __init__(self):
        self.trace_id = None

    def retrieve(self, _query: str, trace_id: str = "") -> str:
        self.trace_id = trace_id
        return ""


def test_empty_rag_requires_uncertainty_and_propagates_trace_id():
    rag = _EmptyRAG()
    server = RAGMCPServer(rag)

    result = server.call_tool(
        "rag_retrieve", {"query": "未知设定"}, trace_id="trace123"
    )

    assert rag.trace_id == "trace123"
    assert "坦诚说明不确定" in result
    assert "不要凭模型记忆" in result


def test_extract_rag_sources_deduplicates_labels():
    result = (
        "[来源: 角色设定.md]\n内容A\n\n---\n\n"
        "[来源: 角色设定.md]\n内容B\n\n---\n\n"
        "[来源: 剧情.md]\n内容C"
    )

    assert _extract_rag_sources(result) == ["角色设定.md", "剧情.md"]


def test_context_manager_returns_actual_injected_memory_metadata():
    class _Memory:
        def retrieve(self, _input: str, trace_id: str = "") -> str:
            return "[相关记忆]\n- 灰宝喜欢晴天"

    config = SimpleNamespace(
        memory=SimpleNamespace(
            enabled=True,
            template="{memory}\n\n{input}",
        )
    )
    manager = ContextManager(config, _Memory())

    enhanced, memories = manager.build_input_with_metadata("早上好", "trace123")

    assert "灰宝喜欢晴天" in enhanced
    assert memories == ["相关记忆"]


def test_persona_defaults_match_calibrated_threshold():
    settings = PersonaSettings()

    assert settings.drift_threshold == 0.48
    assert settings.consecutive_trigger == 3
