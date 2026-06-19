"""RAG 全链路耗时基准测试

测试问题：风堇，你最喜欢翁法罗斯的哪里？
测量环节：
  T0 → T1  API 首次调用（LLM 判断是否调用 RAG）
  T1 → T2  RAG 检索（查询增强 + 向量召回 + 重排序）
  T2 → T3  API 二次调用（LLM 基于检索结果生成回答）
"""

import os
import sys
import time
from pathlib import Path
from functools import wraps

os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import Config, RAGSettings
from src.agent import Agent
from src.rag.rag_service import RAGService
from src.mcp_servers.rag_server import RAGMCPServer

# ── 计时数据收集 ──────────────────────────────────────────

timings: dict = {}


def stamp(label: str):
    """记录时间戳"""
    timings[label] = time.perf_counter()


def report():
    """输出计时报告"""
    print("\n" + "=" * 65)
    print("  RAG Pipeline Latency Report")
    print("=" * 65)

    stages = [
        ("T0_start",          "T1_llm_first_req",   "[1] 1st API call  (LLM decides tool)"),
        ("T1_llm_first_req",  "T1_llm_first_resp",  "     - LLM think + return tool_use"),
        ("T1_llm_first_resp", "T2_tool_exec_start",  "     - Parse tool_use, prepare exec"),
        ("T2_tool_exec_start", "T2_tool_exec_end",   "[2] RAG retrieve  (enhance+recall+rerank)"),
        ("T2_rag_enhance",    "T2_rag_recall",       "     - Query enhance"),
        ("T2_rag_recall",     "T2_rag_rerank",       "     - Vector recall"),
        ("T2_rag_rerank",     "T2_rag_context",      "     - Rerank"),
        ("T2_rag_context",    "T2_tool_exec_end",    "     - Build context"),
        ("T2_tool_exec_end",  "T3_llm_second_req",   "[3] Build 2nd request"),
        ("T3_llm_second_req", "T3_llm_second_resp",  "[4] 2nd API call  (LLM generates answer)"),
        ("T0_start",          "T_end",               "[=] TOTAL"),
    ]

    for start_key, end_key, label in stages:
        if start_key in timings and end_key in timings:
            ms = (timings[end_key] - timings[start_key]) * 1000
            bar = "#" * int(ms / 100) if ms > 0 else ""
            print(f"  {label:45s} {ms:>8.0f} ms  {bar}")

    print("=" * 65)


# ── Monkey-patch 注入计时 ─────────────────────────────────

# 保存原始方法
_original_chat = None
_original_retrieve = None
_original_call_tool = None


def patch_agent(agent: Agent, rag_service: RAGService):
    """给 Agent 和 RAGService 注入计时"""
    global _original_chat, _original_retrieve, _original_call_tool

    # 保存原始方法
    _original_chat = agent.chat
    _original_retrieve = rag_service.retrieve
    _original_call_tool = agent._process_tool_calls

    # ── Patch Agent.chat ──
    def timed_chat(user_input, skills=None):
        stamp("T0_start")

        # 手动重建 chat 逻辑以插入计时点
        from src.utils.logger import generate_trace_id, get_logger

        agent.trace_id = generate_trace_id()
        agent.log = get_logger(agent.trace_id)
        agent.log.info("用户输入: {}...", user_input[:50])

        message_content = user_input
        if skills:
            from src.capabilities.skill import SkillContext
            context = SkillContext(
                trace_id=agent.trace_id,
                user_input=user_input,
                conversation_history=agent.messages,
                config={}
            )
            for skill_name in skills:
                result = agent.registry.execute(skill_name, context)
                if result.success and result.data and "prompt" in result.data:
                    message_content = result.data["prompt"]

        agent.messages.append({"role": "user", "content": message_content})

        tool_definitions = agent.tool_registry.get_all_definitions()

        # ── ① 首次 API 调用 ──
        api_messages = [{"role": "system", "content": agent.config.system_prompt}]
        api_messages.extend(agent.messages)
        api_params = {
            "model": agent.config.model,
            "max_tokens": agent.config.agent.max_tokens,
            "temperature": agent.config.agent.temperature,
            "messages": api_messages,
            "tools": tool_definitions if tool_definitions else None,
        }

        stamp("T1_llm_first_req")
        response = agent._stream_call(api_params)
        stamp("T1_llm_first_resp")

        # 检查是否需要 tool calling
        tool_rounds = 0
        while agent._has_tool_use(response) and tool_rounds < 5:
            tool_rounds += 1

            # ── ② Tool 执行（RAG 检索）──
            stamp("T2_tool_exec_start")
            tool_calls_list, tool_messages = agent._process_tool_calls(response)
            stamp("T2_tool_exec_end")

            agent.messages.append({
                "role": "assistant",
                "content": response.choices[0].message.content or "",
                "tool_calls": tool_calls_list,
            })
            agent.messages.extend(tool_messages)

            # ── ④ 二次 API 调用 ──
            api_params["messages"] = agent._build_api_messages_with_system(agent.config.system_prompt)
            stamp("T3_llm_second_req")
            response = agent._stream_call(api_params)
            stamp("T3_llm_second_resp")

        # 提取最终回复
        assistant_message = agent._extract_text(response)
        agent.messages.append({"role": "assistant", "content": assistant_message})

        stamp("T_end")
        agent.log.info("回复完成，长度: {}", len(assistant_message))
        return assistant_message

    # ── Patch RAGService.retrieve（细粒度计时）──
    def timed_retrieve(query):
        if not rag_service._initialized:
            rag_service.initialize()

        rag_service.log.info("RAG 检索: {}...", query[:50])

        # 查询增强
        stamp("T2_rag_enhance")
        enhanced_query = rag_service.query_enhancer.enhance(query)
        stamp("T2_rag_recall")

        # 召回
        if isinstance(enhanced_query, list):
            all_results = []
            for q in enhanced_query:
                results = rag_service.retriever.retrieve(q)
                all_results.extend(results)
            recall_results = rag_service._deduplicate_results(all_results)
        else:
            recall_results = rag_service.retriever.retrieve(enhanced_query)

        # 重排序
        stamp("T2_rag_rerank")
        reranked_results = rag_service.reranker.rerank(query, recall_results)
        stamp("T2_rag_context")

        # 构建上下文
        context_text = rag_service._build_context(reranked_results, max_length=3000)
        rag_service.log.info(
            f"RAG 检索完成: 召回 {len(recall_results)} 条, 精排 {len(reranked_results)} 条"
        )

        return context_text

    # 注入 patch
    agent.chat = timed_chat
    rag_service.retrieve = timed_retrieve

    return agent


# ── 主流程 ────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("  RAG 全链路耗时基准测试")
    print("  问题: 风堇，你最喜欢翁法罗斯的哪里？")
    print("=" * 65)

    # 加载配置（bench 在 tests/integration/，往上两层到项目根）
    project_root = Path(__file__).parent.parent.parent
    config_path = project_root / "config" / "config.yaml"
    config = Config.load(str(config_path))

    rag_config_path = project_root / "config" / "rag.yaml"
    rag_config = RAGSettings.load(str(rag_config_path))

    # 创建 Agent 和 RAG
    print("\n[初始化] 创建 Agent + RAG 服务...")
    agent = Agent(config)
    rag_service = RAGService(rag_config, llm_client=agent.client)
    rag_server = RAGMCPServer(rag_service)
    agent.register_mcp(rag_server)

    # 注入计时
    patch_agent(agent, rag_service)

    # 确认知识库有数据
    stats = rag_service.get_stats()
    print(f"[知识库] 文档数: {stats['document_count']}")

    # 运行测试（多个问题，观察 LLM 自主决策）
    questions = [
        "风堇，你最喜欢翁法罗斯的哪里？",           # 开放问题，可能不调 RAG
        "风堇，你重返神悟树庭时发现了那刻夏的什么秘密？",  # 剧情细节，应触发 RAG
        "风堇，你和阿格莱雅是怎么认识的？",            # 人物关系，应触发 RAG
    ]

    all_results = []

    for i, question in enumerate(questions):
        timings.clear()
        agent.clear_history()  # 每次独立对话
        print(f"\n{'=' * 65}")
        print(f"  测试 {i+1}/{len(questions)}: {question}")
        print(f"{'=' * 65}")

        response = agent.chat(question)

        used_rag = "T2_tool_exec_start" in timings
        status = "RAG" if used_rag else "直接回答"

        print(f"\n[回答] {response[:200]}...")
        print(f"[路径] {status}")

        report()

        all_results.append({
            "question": question,
            "path": status,
            "timings": dict(timings),
        })

    # 汇总
    print("\n" + "=" * 65)
    print("  汇总")
    print("=" * 65)
    for r in all_results:
        total = (r["timings"]["T_end"] - r["timings"]["T0_start"]) * 1000 if "T_end" in r["timings"] and "T0_start" in r["timings"] else 0
        print(f"  [{r['path']:4s}] {total:>6.0f}ms  {r['question']}")
    print("=" * 65)


if __name__ == "__main__":
    main()
