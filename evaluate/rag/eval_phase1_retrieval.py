"""Phase 1: 纯检索评估

零 API 成本，纯数学计算检索质量指标。

管道：Retriever(top_k=8) → Reranker(top_n=4)
（Phase 1 不使用 QueryEnhancer，保持零 API 成本）

指标：Hit@K, Precision@K, Recall@K, MRR, MAP, nDCG

用法：python evaluate/rag/eval_phase1_retrieval.py
"""

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from src.config import RAGSettings
from src.rag.c_indexer import Indexer
from src.rag.d_retriever import Retriever
from src.rag.f_reranker import Reranker

DATASET_PATH = Path(__file__).parent / "test_dataset.json"
REPORTS_DIR = Path(__file__).parent / "reports"


def load_dataset() -> List[Dict[str, Any]]:
    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def is_relevant(chunk_content: str, relevant_excerpts: List[str]) -> bool:
    """判断 chunk 是否相关：内容中包含任一标注摘录"""
    if not relevant_excerpts:
        return False
    for excerpt in relevant_excerpts:
        if excerpt in chunk_content:
            return True
    return False


def compute_relevance_labels(
    retrieved: List[Any], relevant_excerpts: List[str]
) -> List[int]:
    """返回每个检索结果的相关性标签列表 (1=相关, 0=不相关)"""
    return [1 if is_relevant(r.content, relevant_excerpts) else 0 for r in retrieved]


def estimate_total_relevant(retriever: Retriever, question: str, excerpts: List[str], wide_k: int = 30) -> int:
    """通过宽检索(top_k=30)估计知识库中总相关文档数，作为 Recall 的分母"""
    strategy = retriever._strategy
    original_k = strategy.top_k
    strategy.top_k = wide_k
    wide_results = strategy.retrieve(question)
    strategy.top_k = original_k
    wide_labels = compute_relevance_labels(wide_results, excerpts)
    return max(sum(wide_labels), 1)


def calc_hit_at_k(labels: List[int], k: int) -> float:
    """前 K 个中是否至少命中一个相关文档"""
    if not labels:
        return 0.0
    return 1.0 if sum(labels[:k]) > 0 else 0.0


def calc_precision_at_k(labels: List[int], k: int) -> float:
    """Precision@K = 前 K 个中相关数 / K"""
    if k == 0:
        return 0.0
    return sum(labels[:k]) / k


def calc_recall_at_k(labels: List[int], k: int, total_relevant: int) -> float:
    """Recall@K = 前 K 个中找到的相关数 / 总相关数"""
    if total_relevant == 0:
        return 0.0
    return sum(labels[:k]) / total_relevant


def calc_mrr(labels: List[int]) -> float:
    """MRR = 1 / 第一个相关结果的排名"""
    for i, label in enumerate(labels):
        if label == 1:
            return 1.0 / (i + 1)
    return 0.0


def calc_ap(labels: List[int], total_relevant: int) -> float:
    """Average Precision for one query"""
    if total_relevant == 0:
        return 0.0
    hits = 0
    sum_precision = 0.0
    for i, label in enumerate(labels):
        if label == 1:
            hits += 1
            sum_precision += hits / (i + 1)
    return sum_precision / total_relevant


def calc_ndcg_at_k(labels: List[int], k: int) -> float:
    """nDCG@K"""
    if not labels:
        return 0.0
    dcg = 0.0
    for i in range(min(k, len(labels))):
        if labels[i] == 1:
            dcg += 1.0 / math.log2(i + 2)
    ideal_labels = sorted(labels, reverse=True)
    idcg = 0.0
    for i in range(min(k, len(ideal_labels))):
        if ideal_labels[i] == 1:
            idcg += 1.0 / math.log2(i + 2)
    return dcg / idcg if idcg > 0 else 0.0


def evaluate_retrieval(
    retriever: Retriever,
    reranker: Reranker,
    dataset: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """对整个测试集评估所有检索指标

    管道：Retriever(top_k=8) → Reranker(top_n=4)
    指标基于重排序后的 4 个结果计算。
    """
    results_per_query = []
    top_n = 4

    for item in dataset:
        question = item["question"]
        excerpts = item.get("relevant_excerpts", [])
        category = item.get("category", "未分类")

        if not excerpts:
            continue

        # 宽检索估计总相关文档数（用于 Recall 分母）
        total_relevant = estimate_total_relevant(retriever, question, excerpts)

        # 正式管道：Retriever → Reranker
        retrieved = retriever.retrieve(question)
        reranked = reranker.rerank(question, retrieved)
        labels = compute_relevance_labels(reranked, excerpts)

        results_per_query.append({
            "id": item["id"],
            "question": question,
            "category": category,
            "num_retrieved": len(reranked),
            "num_relevant_found": sum(labels),
            "total_relevant": total_relevant,
            "labels": labels,
            "hit_3": calc_hit_at_k(labels, 3),
            "hit_4": calc_hit_at_k(labels, top_n),
            "precision_3": calc_precision_at_k(labels, 3),
            "precision_4": calc_precision_at_k(labels, top_n),
            "recall_4": calc_recall_at_k(labels, top_n, total_relevant),
            "mrr": calc_mrr(labels),
            "ap": calc_ap(labels, total_relevant),
            "ndcg_3": calc_ndcg_at_k(labels, 3),
            "ndcg_4": calc_ndcg_at_k(labels, top_n),
        })

    n = len(results_per_query)
    if n == 0:
        return {"error": "No valid queries to evaluate"}

    aggregates = {
        "num_queries": n,
        "hit_3": sum(r["hit_3"] for r in results_per_query) / n,
        "hit_4": sum(r["hit_4"] for r in results_per_query) / n,
        "precision_3": sum(r["precision_3"] for r in results_per_query) / n,
        "precision_4": sum(r["precision_4"] for r in results_per_query) / n,
        "recall_4": sum(r["recall_4"] for r in results_per_query) / n,
        "mrr": sum(r["mrr"] for r in results_per_query) / n,
        "map": sum(r["ap"] for r in results_per_query) / n,
        "ndcg_3": sum(r["ndcg_3"] for r in results_per_query) / n,
        "ndcg_4": sum(r["ndcg_4"] for r in results_per_query) / n,
    }

    categories = {}
    for r in results_per_query:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(r)

    category_metrics = {}
    for cat, queries in categories.items():
        cn = len(queries)
        category_metrics[cat] = {
            "count": cn,
            "hit_3": sum(q["hit_3"] for q in queries) / cn,
            "precision_3": sum(q["precision_3"] for q in queries) / cn,
            "recall_4": sum(q["recall_4"] for q in queries) / cn,
            "mrr": sum(q["mrr"] for q in queries) / cn,
        }

    return {
        "aggregates": aggregates,
        "per_query": results_per_query,
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
        f"# RAG 评估报告 — Phase 1：纯检索评估",
        f"",
        f"**日期**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**评估阶段**：Phase 1（纯计算，零 API 成本）",
        f"**测试集规模**：{agg['num_queries']} 条",
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
        "## 检索指标总览",
        "",
        "| 指标 | 得分 | 过关线 | 状态 |",
        "|------|------|--------|------|",
    ]

    thresholds = {
        "Hit@3": (agg["hit_3"], 0.80),
        "Hit@4": (agg["hit_4"], 0.70),
        "Precision@3": (agg["precision_3"], 0.40),
        "Precision@4": (agg["precision_4"], 0.35),
        "Recall@4": (agg["recall_4"], 0.50),
        "MRR": (agg["mrr"], 0.60),
        "MAP": (agg["map"], 0.50),
        "nDCG@3": (agg["ndcg_3"], 0.50),
        "nDCG@4": (agg["ndcg_4"], 0.50),
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
        "| 类别 | 数量 | Hit@3 | Precision@3 | Recall@4 | MRR |",
        "|------|------|-------|-------------|----------|-----|",
    ]
    for cat_name, cat_metrics in sorted(cat.items()):
        lines.append(
            f"| {cat_name} | {cat_metrics['count']} | "
            f"{cat_metrics['hit_3']:.4f} | {cat_metrics['precision_3']:.4f} | "
            f"{cat_metrics['recall_4']:.4f} | {cat_metrics['mrr']:.4f} |"
        )

    failed_queries = [q for q in per_query if q["hit_4"] == 0.0]
    if failed_queries:
        lines += [
            "",
            "---",
            "",
            "## 未命中查询（Hit@4 = 0）",
            "",
        ]
        for q in failed_queries:
            lines.append(f"- [{q['category']}] {q['question']}")

    lines += [
        "",
        "---",
        "",
        "## 诊断",
        "",
    ]
    if all_pass:
        lines.append("所有指标均过关，检索地基健康，可进入 Phase 2。")
    else:
        failed_names = [n for n, (s, t) in thresholds.items() if s < t]
        lines.append(f"未达标指标：{', '.join(failed_names)}")
        lines.append("")
        if agg["hit_3"] < 0.80:
            lines.append("- Hit@3 低：检索器几乎找不到相关文档 → 检查 Embedding 模型、分块策略")
        if agg["hit_3"] >= 0.80 and agg["mrr"] < 0.60:
            lines.append("- Hit@3 正常但 MRR 低：能找到但排得靠后 → 调 Reranker 参数")
        if agg["recall_4"] < 0.50:
            lines.append("- Recall@4 低：Reranker 后仍有遗漏 → 增大 top_n 或优化 Reranker")

    return "\n".join(lines)


def main():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    dataset = load_dataset()
    print(f"加载测试集：{len(dataset)} 条")

    print("初始化组件...")
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

    reranker = Reranker(
        strategy_type=rag_cfg.reranker.type,
        strategy_params=rag_cfg.reranker.params,
    )
    reranker.initialize()
    print("检索器 + 重排序器初始化完成\n")

    print("开始评估...")
    evaluation = evaluate_retrieval(retriever, reranker, dataset)

    config_snapshot = {
        "Embedding 模型": rag_cfg.index.params.get("embedding_model", "N/A"),
        "检索策略": f"{rag_cfg.retriever.type} (Dense {rag_cfg.index.params.get('dense_weight', 0.7)} + Sparse {rag_cfg.index.params.get('sparse_weight', 0.3)})",
        "Top-K": str(rag_cfg.retriever.params.get("top_k", 8)),
        "重排序": f"{rag_cfg.reranker.type} ({rag_cfg.reranker.params.get('model', 'N/A')}, top_n={rag_cfg.reranker.params.get('top_n', 4)})",
        "分块策略": f"{rag_cfg.splitter.type} (max_chunk={rag_cfg.splitter.params.get('max_chunk_size', 1500)})",
    }

    report = generate_report(evaluation, config_snapshot)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORTS_DIR / f"phase1_retrieval_{timestamp}.md"
    report_path.write_text(report, encoding="utf-8")

    agg = evaluation["aggregates"]
    print(f"\n{'='*50}")
    print(f"Phase 1 评估完成！")
    print(f"{'='*50}")
    print(f"  测试条数: {agg['num_queries']}")
    print(f"  Hit@3:    {agg['hit_3']:.4f}")
    print(f"  Hit@4:    {agg['hit_4']:.4f}")
    print(f"  P@3:      {agg['precision_3']:.4f}")
    print(f"  P@4:      {agg['precision_4']:.4f}")
    print(f"  R@4:      {agg['recall_4']:.4f}")
    print(f"  MRR:      {agg['mrr']:.4f}")
    print(f"  MAP:      {agg['map']:.4f}")
    print(f"  nDCG@3:   {agg['ndcg_3']:.4f}")
    print(f"  nDCG@4:   {agg['ndcg_4']:.4f}")
    print(f"\n报告已保存: {report_path}")

    indexer.cleanup()
    reranker.cleanup()


if __name__ == "__main__":
    main()
