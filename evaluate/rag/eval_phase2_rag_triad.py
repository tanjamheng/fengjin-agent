"""Phase 2: RAG Triad 评估（LLM-as-Judge）

使用 LLM 作为裁判，评估 RAG 全流程质量。

管道：QueryEnhancer → Retriever(top_k=8) → Reranker(top_n=4) → LLM 生成回答 → LLM 裁判评分

指标（按重要度）：Faithfulness, Context Recall, Answer Relevancy,
                 Context Relevancy, Context Precision

用法：python evaluate/rag/eval_phase2_rag_triad.py
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from src.config import RAGSettings
from src.rag.c_indexer import Indexer
from src.rag.d_retriever import Retriever
from src.rag.e_query_enhancer import QueryEnhancer
from src.rag.f_reranker import Reranker

load_dotenv(project_root / ".env")

DATASET_PATH = Path(__file__).parent / "test_dataset.json"
REPORTS_DIR = Path(__file__).parent / "reports"

LLM_MODEL = os.getenv("EVAL_LLM_MODEL", "GLM-5.1")

REFUSAL_PATTERNS = [
    "根据已有信息无法回答该问题",
    "无法回答",
    "没有足够的信息",
    "无法提供",
]


def get_llm_client():
    """获取 LLM 客户端"""
    from openai import OpenAI

    api_key = os.getenv("FENGJIN_API_KEY")
    base_url = os.getenv("FENGJIN_BASE_URL")

    if not api_key:
        raise ValueError("请在 .env 中设置 FENGJIN_API_KEY")

    return OpenAI(api_key=api_key, base_url=base_url)


def call_llm(client, system_prompt: str, user_prompt: str, max_retries: int = 2) -> str:
    """调用 LLM，带重试"""
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                max_tokens=2048,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return response.choices[0].message.content
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            else:
                raise


def is_refusal(answer: str) -> bool:
    """判断回答是否为拒绝回答"""
    return any(p in answer for p in REFUSAL_PATTERNS)


def generate_rag_answer(client, question: str, contexts: str) -> str:
    """用 LLM 基于 RAG 检索上下文生成回答"""
    system = (
        "你是一个知识问答助手。请严格基于给定的上下文来回答问题。"
        "如果上下文中没有足够的信息回答该问题，请直接说\"根据已有信息无法回答该问题\"。"
        "不要编造上下文中没有的信息。"
    )
    user = f"上下文：\n{contexts}\n\n问题：{question}\n\n请基于上下文回答："
    return call_llm(client, system, user)


def format_contexts(retrieved_results) -> str:
    """格式化检索结果为上下文字符串"""
    parts = []
    for i, r in enumerate(retrieved_results, 1):
        source = r.source or "未知来源"
        parts.append(f"[文档{i} - {source}]\n{r.content}")
    return "\n\n---\n\n".join(parts)


def retrieve_with_pipeline(
    question: str,
    query_enhancer: QueryEnhancer,
    retriever: Retriever,
    reranker: Reranker,
) -> List:
    """执行完整检索管道：QueryEnhancer → Retriever → Reranker"""
    enhanced = query_enhancer.enhance(question)

    if isinstance(enhanced, list):
        all_results = []
        seen_contents = set()
        for q in enhanced:
            results = retriever.retrieve(q)
            for r in results:
                if r.content not in seen_contents:
                    seen_contents.add(r.content)
                    all_results.append(r)
        recall_results = all_results
    else:
        recall_results = retriever.retrieve(enhanced)

    reranked = reranker.rerank(question, recall_results)
    return reranked


def eval_faithfulness(client, question: str, answer: str, contexts: str) -> Dict[str, Any]:
    """评估 Faithfulness（忠实度）

    修复：拒绝回答("无法回答")视为忠实（没有编造），直接得 1.0
    """
    if is_refusal(answer):
        return {"score": 1.0, "supported": 0, "total": 0, "claims": [], "note": "拒绝回答，视为忠实"}

    system = (
        "你是一个严格的事实核查员。你的任务是检查回答中的每条信息是否能在给定的上下文中找到依据。"
        "请严格按照以下格式输出，不要输出其他内容：\n"
        "CLAIMS:\n"
        "1. [陈述内容] - SUPPORTED 或 NOT_SUPPORTED\n"
        "2. [陈述内容] - SUPPORTED 或 NOT_SUPPORTED\n"
        "...\n"
        "SCORE: X/Y"
    )
    user = (
        f"上下文：\n{contexts}\n\n"
        f"问题：{question}\n\n"
        f"回答：{answer}\n\n"
        f"请将回答拆解为若干条独立的陈述，逐条判断每条是否能从上下文中找到依据。"
    )
    response = call_llm(client, system, user)
    return _parse_faithfulness(response)


def _parse_faithfulness(response: str) -> Dict[str, Any]:
    """解析 Faithfulness 评分"""
    supported = 0
    total = 0
    claims = []
    for line in response.split("\n"):
        line = line.strip()
        if "SUPPORTED" in line.upper() and "NOT_SUPPORTED" not in line.upper():
            supported += 1
            total += 1
            claims.append({"claim": line.split("-")[0].strip().lstrip("0123456789. "), "supported": True})
        elif "NOT_SUPPORTED" in line.upper():
            total += 1
            claim_text = line.split("-")[0].strip().lstrip("0123456789. ")
            claims.append({"claim": claim_text, "supported": False})

    if total == 0:
        score_match = re.search(r"SCORE:\s*(\d+)/(\d+)", response)
        if score_match:
            supported = int(score_match.group(1))
            total = int(score_match.group(2))
        else:
            return {"score": 0.0, "supported": 0, "total": 0, "claims": [], "raw": response}

    score = supported / total if total > 0 else 0.0
    return {"score": score, "supported": supported, "total": total, "claims": claims}


def eval_context_recall(client, question: str, contexts: str, ground_truth: str) -> Dict[str, Any]:
    """评估 Context Recall（上下文召回率）"""
    system = (
        "你是一个信息检索评估专家。你的任务是检查标准答案中的每个关键信息点是否能在检索到的上下文中找到。"
        "请严格按照以下格式输出：\n"
        "FACTS:\n"
        "1. [信息点] - FOUND 或 NOT_FOUND\n"
        "2. [信息点] - FOUND 或 NOT_FOUND\n"
        "...\n"
        "SCORE: X/Y"
    )
    user = (
        f"上下文：\n{contexts}\n\n"
        f"问题：{question}\n\n"
        f"标准答案：{ground_truth}\n\n"
        f"请将标准答案拆解为若干个关键信息点，逐个判断每个信息点是否能在上下文中找到。"
    )
    response = call_llm(client, system, user)
    return _parse_binary_score(response, "found", "not_found")


def eval_answer_relevancy(client, question: str, answer: str) -> Dict[str, Any]:
    """评估 Answer Relevancy（答案相关性）"""
    system = (
        "你是一个评估专家。请评估回答是否切题地回应了用户的问题。"
        "评分标准：\n"
        "1.0 = 完全切题，直接回答了问题\n"
        "0.8 = 基本切题，有小部分偏离\n"
        "0.6 = 部分切题，但包含较多无关内容\n"
        "0.4 = 回答了部分问题，但大量内容无关\n"
        "0.2 = 大部分不切题\n"
        "0.0 = 完全答非所问\n\n"
        "注意：如果回答是\"无法回答\"类的内容，且问题确实超出了知识范围，"
        "这属于正确行为，应评 1.0 分。\n\n"
        "请只输出一个 0.0 到 1.0 之间的数字。"
    )
    user = f"问题：{question}\n\n回答：{answer}\n\n评分（0.0-1.0）："
    response = call_llm(client, system, user)
    score = _extract_score(response)
    return {"score": score, "raw": response.strip()}


def eval_context_relevancy(client, question: str, contexts: str) -> Dict[str, Any]:
    """评估 Context Relevancy（上下文相关性）"""
    system = (
        "你是一个评估专家。请评估每个检索文档与问题的相关性。"
        "对每个文档，判断其是否包含回答问题所需的信息。\n"
        "请严格按照以下格式输出：\n"
        "DOC_1: RELEVANT 或 NOT_RELEVANT\n"
        "DOC_2: RELEVANT 或 NOT_RELEVANT\n"
        "...\n"
        "SCORE: X/Y (X个相关, Y个总共)"
    )
    docs = contexts.split("\n\n---\n\n")
    user = f"问题：{question}\n\n"
    for i, doc in enumerate(docs, 1):
        user += f"DOC_{i}:\n{doc}\n\n"
    user += "请逐个判断每个文档的相关性。"

    response = call_llm(client, system, user)
    return _parse_binary_score(response, "relevant", "not_relevant")


def eval_context_precision(client, question: str, contexts: str, ground_truth: str) -> Dict[str, Any]:
    """评估 Context Precision（上下文精确率）

    使用位置加权公式（RAGAS 定义）：
    Context Precision = Σ(前i个中相关数 / i) / 总相关数
    只对相关位置求和，相关文档排得越靠前分数越高。
    """
    system = (
        "你是一个评估专家。请评估每个检索文档与问题的相关性，注意相关文档的排序位置。\n"
        "请严格按照以下格式输出：\n"
        "DOC_1: RELEVANT 或 NOT_RELEVANT\n"
        "DOC_2: RELEVANT 或 NOT_RELEVANT\n"
        "...\n"
        "SCORE: X/Y"
    )
    docs = contexts.split("\n\n---\n\n")
    user = f"问题：{question}\n\n标准答案：{ground_truth}\n\n"
    for i, doc in enumerate(docs, 1):
        user += f"DOC_{i}:\n{doc}\n\n"
    user += "请逐个判断每个文档是否包含回答问题所需的信息。"

    response = call_llm(client, system, user)
    relevance_labels = _parse_relevance_labels(response, len(docs))
    return _calc_context_precision(relevance_labels)


def _parse_relevance_labels(response: str, num_docs: int) -> List[int]:
    """从 LLM 响应中提取每个文档的相关性标签"""
    labels = []
    for i in range(1, num_docs + 1):
        pattern = re.compile(rf"DOC_{i}\s*:\s*(RELEVANT|NOT_RELEVANT)", re.IGNORECASE)
        match = pattern.search(response)
        if match:
            labels.append(1 if match.group(1).upper() == "RELEVANT" else 0)
        else:
            labels.append(0)
    return labels


def _calc_context_precision(labels: List[int]) -> Dict[str, Any]:
    """计算位置加权的 Context Precision（RAGAS 公式）

    公式：CP = Σ(k=1..n, where label[k]=1)(Precision@k) / 总相关数
    例：labels = [1,0,1,0] → (1/1 + 2/3) / 2 = 0.833
    """
    total_relevant = sum(labels)
    if total_relevant == 0:
        return {"score": 0.0, "positive": 0, "total": 0}

    weighted_sum = 0.0
    for k in range(len(labels)):
        if labels[k] == 1:
            precision_at_k = sum(labels[:k + 1]) / (k + 1)
            weighted_sum += precision_at_k

    score = weighted_sum / total_relevant
    return {"score": score, "positive": total_relevant, "total": len(labels)}


def _parse_binary_score(response: str, positive_key: str, negative_key: str) -> Dict[str, Any]:
    """解析二元评分（FOUND/NOT_FOUND, RELEVANT/NOT_RELEVANT 等）"""
    positive = 0
    total = 0
    positive_upper = positive_key.upper()
    negative_upper = negative_key.upper()

    for line in response.split("\n"):
        line_upper = line.upper().strip()
        if positive_upper in line_upper and negative_upper not in line_upper:
            positive += 1
            total += 1
        elif negative_upper in line_upper:
            total += 1

    if total == 0:
        score_match = re.search(r"SCORE:\s*(\d+)/(\d+)", response)
        if score_match:
            positive = int(score_match.group(1))
            total = int(score_match.group(2))
        else:
            return {"score": 0.0, "positive": 0, "total": 0, "raw": response}

    score = positive / total if total > 0 else 0.0
    return {"score": score, "positive": positive, "total": total}


def _extract_score(response: str) -> float:
    """从 LLM 响应中提取 0-1 的分数"""
    numbers = re.findall(r"[0-9]*\.?[0-9]+", response)
    if numbers:
        score = float(numbers[0])
        return max(0.0, min(1.0, score))
    return 0.0


def evaluate_rag_triad(
    client,
    retriever: Retriever,
    reranker: Reranker,
    query_enhancer: QueryEnhancer,
    dataset: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """对整个测试集评估 RAG Triad 指标"""
    per_query = []

    total = len(dataset)
    for idx, item in enumerate(dataset):
        question = item["question"]
        ground_truth = item["ground_truth"]
        category = item.get("category", "未分类")

        print(f"  [{idx+1}/{total}] {question[:40]}...")

        # 完整管道：QueryEnhancer → Retriever → Reranker
        reranked = retrieve_with_pipeline(question, query_enhancer, retriever, reranker)
        contexts = format_contexts(reranked)

        answer = generate_rag_answer(client, question, contexts)

        query_result = {
            "id": item["id"],
            "question": question,
            "category": category,
            "answer": answer,
            "ground_truth": ground_truth,
            "num_contexts": len(reranked),
            "metrics": {},
        }

        print(f"    → Faithfulness...", end=" ", flush=True)
        faith = eval_faithfulness(client, question, answer, contexts)
        query_result["metrics"]["faithfulness"] = faith["score"]
        print(f"{faith['score']:.2f}")

        print(f"    → Context Recall...", end=" ", flush=True)
        ctx_recall = eval_context_recall(client, question, contexts, ground_truth)
        query_result["metrics"]["context_recall"] = ctx_recall["score"]
        print(f"{ctx_recall['score']:.2f}")

        print(f"    → Answer Relevancy...", end=" ", flush=True)
        ans_rel = eval_answer_relevancy(client, question, answer)
        query_result["metrics"]["answer_relevancy"] = ans_rel["score"]
        print(f"{ans_rel['score']:.2f}")

        print(f"    → Context Relevancy...", end=" ", flush=True)
        ctx_rel = eval_context_relevancy(client, question, contexts)
        query_result["metrics"]["context_relevancy"] = ctx_rel["score"]
        print(f"{ctx_rel['score']:.2f}")

        print(f"    → Context Precision...", end=" ", flush=True)
        ctx_prec = eval_context_precision(client, question, contexts, ground_truth)
        query_result["metrics"]["context_precision"] = ctx_prec["score"]
        print(f"{ctx_prec['score']:.2f}")

        per_query.append(query_result)

    n = len(per_query)
    if n == 0:
        return {"error": "No queries evaluated"}

    metric_names = ["faithfulness", "context_recall", "answer_relevancy", "context_relevancy", "context_precision"]
    aggregates = {}
    for m in metric_names:
        values = [q["metrics"][m] for q in per_query]
        aggregates[m] = sum(values) / len(values)

    categories = {}
    for q in per_query:
        cat = q["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(q)

    category_metrics = {}
    for cat, queries in categories.items():
        cn = len(queries)
        category_metrics[cat] = {"count": cn}
        for m in metric_names:
            values = [q["metrics"][m] for q in queries]
            category_metrics[cat][m] = sum(values) / len(values)

    return {
        "aggregates": aggregates,
        "per_query": per_query,
        "category_metrics": category_metrics,
    }


def generate_report(
    evaluation: Dict[str, Any], config_snapshot: Dict[str, str]
) -> str:
    """生成 Markdown 评估报告"""
    agg = evaluation["aggregates"]
    cat = evaluation.get("category_metrics", {})
    per_query = evaluation.get("per_query", [])

    lines = [
        f"# RAG 评估报告 — Phase 2：RAG Triad（LLM-as-Judge）",
        f"",
        f"**日期**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**评估阶段**：Phase 2（LLM-as-Judge）",
        f"**测试集规模**：{len(per_query)} 条",
        f"**裁判模型**：{LLM_MODEL}",
        f"",
        f"---",
        f"",
        f"## 配置快照",
        f"",
    ]
    for key, value in config_snapshot.items():
        lines.append(f"- **{key}**：{value}")

    lines += [
        "",
        "---",
        "",
        "## RAG Triad 指标总览",
        "",
        "| 指标 | 得分 | 过关线 | 状态 |",
        "|------|------|--------|------|",
    ]

    thresholds = {
        "Faithfulness": (agg["faithfulness"], 0.70),
        "Context Recall": (agg["context_recall"], 0.60),
        "Answer Relevancy": (agg["answer_relevancy"], 0.60),
        "Context Relevancy": (agg["context_relevancy"], 0.50),
        "Context Precision": (agg["context_precision"], 0.50),
    }

    all_pass = True
    for name, (score, threshold) in thresholds.items():
        status = "PASS" if score >= threshold else "FAIL"
        if status == "FAIL":
            all_pass = False
        lines.append(f"| {name} | {score:.4f} | ≥ {threshold:.2f} | {status} |")

    lines += [
        "",
        "---",
        "",
        "## 按类别分析",
        "",
        "| 类别 | 数量 | Faithfulness | Context Recall | Ans Relevancy |",
        "|------|------|-------------|----------------|---------------|",
    ]
    for cat_name, cat_metrics in sorted(cat.items()):
        lines.append(
            f"| {cat_name} | {cat_metrics['count']} | "
            f"{cat_metrics['faithfulness']:.4f} | {cat_metrics['context_recall']:.4f} | "
            f"{cat_metrics['answer_relevancy']:.4f} |"
        )

    low_queries = [q for q in per_query if q["metrics"]["faithfulness"] < 0.70]
    if low_queries:
        lines += [
            "",
            "---",
            "",
            "## 低忠实度查询（Faithfulness < 0.70）",
            "",
        ]
        for q in low_queries:
            lines.append(f"- [{q['category']}] {q['question']}")
            lines.append(f"  - Faithfulness: {q['metrics']['faithfulness']:.2f}")

    lines += [
        "",
        "---",
        "",
        "## 诊断",
        "",
    ]
    if all_pass:
        lines.append("所有 RAG Triad 指标均过关，RAG 全流程健康。")
        lines.append("可进入 Phase 3 精细化迭代优化。")
    else:
        lines.append("### 诊断矩阵分析")
        lines.append("")
        f_val = agg["faithfulness"]
        cr_val = agg["context_recall"]
        ar_val = agg["answer_relevancy"]
        crel_val = agg["context_relevancy"]

        if crel_val < 0.50:
            lines.append("- Context Relevancy 不达标 → 检索问题 → 回 Phase 1 修检索")
        elif f_val < 0.70:
            if cr_val < 0.60:
                lines.append("- Context Recall 低 + Faithfulness 低 → 检索遗漏信息 → 增 top-K / 优化分块")
            else:
                lines.append("- Context 正常 + Faithfulness 低 → LLM 胡编 → 换模型 / 修 Prompt")
        elif ar_val < 0.60:
            lines.append("- Faithfulness 正常 + Answer Relevancy 低 → 答非所问 → 修 Prompt")

    lines += [
        "",
        "---",
        "",
        "## 逐条详情",
        "",
    ]
    for q in per_query:
        m = q["metrics"]
        lines.append(f"### Q{q['id']}: {q['question']}")
        lines.append(f"- 类别: {q['category']}")
        lines.append(f"- Faithfulness: {m['faithfulness']:.2f} | Context Recall: {m['context_recall']:.2f} | Ans Relevancy: {m['answer_relevancy']:.2f}")
        lines.append(f"- RAG 回答: {q['answer'][:200]}...")
        lines.append("")

    return "\n".join(lines)


def main():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    dataset = load_dataset()
    print(f"加载测试集：{len(dataset)} 条")

    print("初始化组件...")
    client = get_llm_client()
    print(f"  LLM 客户端: {LLM_MODEL}")

    config = RAGSettings.load()
    rag_cfg = config.rag

    indexer = Indexer(
        strategy_type=rag_cfg.index.type,
        strategy_params=rag_cfg.index.params,
    )
    indexer.initialize()

    retriever = Retriever(
        index=indexer._strategy,
        strategy_type=rag_cfg.retriever.type,
        strategy_params=rag_cfg.retriever.params,
    )
    retriever.initialize()

    query_enhancer = QueryEnhancer(
        strategy_type=rag_cfg.query_enhancer.type,
        strategy_params=rag_cfg.query_enhancer.params,
        llm_client=client,
    )
    query_enhancer.initialize()

    reranker = Reranker(
        strategy_type=rag_cfg.reranker.type,
        strategy_params=rag_cfg.reranker.params,
    )
    reranker.initialize()
    print("检索器 + 查询增强 + 重排序器初始化完成\n")

    print("开始 Phase 2 评估（LLM-as-Judge）...\n")
    evaluation = evaluate_rag_triad(client, retriever, reranker, query_enhancer, dataset)

    config_snapshot = {
        "Embedding 模型": rag_cfg.index.params.get("embedding_model", "N/A"),
        "检索策略": f"{rag_cfg.retriever.type} (Dense {rag_cfg.index.params.get('dense_weight', 0.7)} + Sparse {rag_cfg.index.params.get('sparse_weight', 0.3)})",
        "Top-K": str(rag_cfg.retriever.params.get("top_k", 8)),
        "重排序": f"{rag_cfg.reranker.type} ({rag_cfg.reranker.params.get('model', 'N/A')}, top_n={rag_cfg.reranker.params.get('top_n', 4)})",
        "查询增强": rag_cfg.query_enhancer.type,
        "分块策略": f"{rag_cfg.splitter.type} (max_chunk={rag_cfg.splitter.params.get('max_chunk_size', 1500)})",
        "裁判 LLM": LLM_MODEL,
    }

    report = generate_report(evaluation, config_snapshot)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORTS_DIR / f"phase2_rag_triad_{timestamp}.md"
    report_path.write_text(report, encoding="utf-8")

    agg = evaluation["aggregates"]
    print(f"\n{'='*50}")
    print(f"Phase 2 评估完成！")
    print(f"{'='*50}")
    print(f"  Faithfulness:      {agg['faithfulness']:.4f}")
    print(f"  Context Recall:    {agg['context_recall']:.4f}")
    print(f"  Answer Relevancy:  {agg['answer_relevancy']:.4f}")
    print(f"  Context Relevancy: {agg['context_relevancy']:.4f}")
    print(f"  Context Precision: {agg['context_precision']:.4f}")
    print(f"\n报告已保存: {report_path}")

    indexer.cleanup()
    reranker.cleanup()


def load_dataset() -> List[Dict[str, Any]]:
    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    main()
