"""记忆系统完整评估脚本

一条龙评估所有记忆系统指标，输出实验报告。

评估维度：
1. 写入阶段（客观+主观）
   - 提取覆盖率 (GT Recall / Strict Recall)
   - 事实准确性 (Faithfulness / Hallucination Rate)
   - 重要性判断准确率
   - 去重准确率
   - 冲突检测与合并质量
   - Core 记忆完整性
   - PII 过滤率
   - 写入延迟
2. 检索阶段（客观+主观）
   - Recall@K / Hit Rate / MRR
   - 检索延迟
   - 答案相关性

用法：python evaluate/memory/eval_memory_system.py
"""

import json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

load_dotenv(project_root / ".env")

from src.memory.config import MemoryConfig, MemorySettings
from src.memory.storage import MemoryStorage
from src.memory.extractor import MemoryExtractor
from src.memory.writer import MemoryWriter
from src.memory.retriever import MemoryRetriever

from test_data import (
    EXTRACTION_TEST,
    IMPORTANCE_TEST,
    DEDUP_TEST,
    CONFLICT_TEST,
    PII_TEST,
    RETRIEVAL_INITIAL_MEMORIES,
    RETRIEVAL_TEST,
)

REPORTS_DIR = Path(__file__).parent / "reports"
TEST_CHROMA_DIR = "data/test_eval_memory_chroma"
TEST_COLLECTION = "eval_memories"


# ──────────────────────────────────────────────
# LLM Judge 工具函数（复用 RAG 评估模式）
# ──────────────────────────────────────────────

def get_judge_client():
    """获取 LLM judge 客户端（使用记忆辅助模型的 OpenAI 接口）"""
    from openai import OpenAI

    api_key = os.getenv("MIND_API_KEY")
    base_url = os.getenv("MIND_BASE_URL")
    if not api_key:
        raise ValueError("请在 .env 中设置 MIND_API_KEY")

    return OpenAI(api_key=api_key, base_url=base_url)


def call_judge(client, system_prompt: str, user_prompt: str, max_retries: int = 2) -> str:
    model = os.getenv("MIND_MODEL")
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            else:
                return json.dumps({"error": str(e)})


def parse_json_safe(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"error": "parse_failed", "raw": text[:200]}


# ──────────────────────────────────────────────
# 1. 提取覆盖率评估
# ──────────────────────────────────────────────

JUDGE_GT_MATCH = """你是记忆提取评估专家。判断提取的事实是否覆盖了 ground truth。

ground_truth: {gt}
提取结果:
{extracted_facts}

对这条 ground truth，判断：
- match: 完全覆盖，核心信息一致
- partial: 部分覆盖，核心信息存在但细节缺失
- no_match: 未覆盖

输出 JSON:
{{"judgment": "match/partial/no_match", "reason": "一句话说明"}}"""


def eval_extraction_coverage(extractor, judge_client) -> dict:
    """评估提取覆盖率（GT Recall + Strict Recall）"""
    print("\n[1/11] 评估提取覆盖率...")
    results = []

    for item in EXTRACTION_TEST:
        facts = extractor.extract(item["user"], item["assistant"])
        extracted_texts = [f["content"] for f in facts] if facts else []

        if not item["ground_truths"]:
            results.append({
                "id": item["id"],
                "category": item["category"],
                "ground_truths": [],
                "extracted": extracted_texts,
                "gt_recall": 1.0,
                "strict_recall": 1.0,
            })
            continue

        match_count = 0
        partial_count = 0
        for gt in item["ground_truths"]:
            prompt = JUDGE_GT_MATCH.format(
                gt=gt,
                extracted_facts="\n".join(f"- {t}" for t in extracted_texts) if extracted_texts else "(无提取结果)"
            )
            resp = call_judge(judge_client, "你是评估专家，只输出合法JSON。", prompt)
            parsed = parse_json_safe(resp)
            judgment = parsed.get("judgment", "no_match")
            if judgment == "match":
                match_count += 1
            elif judgment == "partial":
                partial_count += 1

        total_gt = len(item["ground_truths"])
        gt_recall = (match_count + partial_count) / total_gt
        strict_recall = match_count / total_gt

        results.append({
            "id": item["id"],
            "category": item["category"],
            "ground_truths": item["ground_truths"],
            "extracted": extracted_texts,
            "gt_recall": gt_recall,
            "strict_recall": strict_recall,
        })
        print(f"  {item['id']} ({item['category']}): GT Recall={gt_recall:.2f}, Strict={strict_recall:.2f}")

    n = len(results)
    avg_gt_recall = sum(r["gt_recall"] for r in results) / n
    avg_strict_recall = sum(r["strict_recall"] for r in results) / n

    return {
        "gt_recall": avg_gt_recall,
        "strict_recall": avg_strict_recall,
        "per_item": results,
    }


# ──────────────────────────────────────────────
# 2. 事实准确性评估
# ──────────────────────────────────────────────

JUDGE_FACT_ACCURACY = """判断提取的事实是否忠实于原始对话。

重要背景：这是角色 AI 系统，用户自称"我"，系统将用户称为"灰宝"。
因此提取的事实会以"灰宝"代替"我"，这是正常替换，不是幻觉。

对话内容：
用户: {user_input}
助手: {assistant_msg}

提取的事实: {fact}

判断标准（"我"被替换为"灰宝"是正常的，不算幻觉）：
1. faithful: 事实中的信息是否全部来源于对话？（除了"我"→"灰宝"的替换外，没有凭空添加信息）
2. accurate: 事实的表述是否准确反映了对话内容？（没有曲解原意）
3. hallucination: 事实是否包含对话中完全没有提到的信息？

输出 JSON:
{{"faithful": true/false, "accurate": true/false, "hallucination": true/false, "reason": "一句话说明"}}"""


def eval_fact_accuracy(extractor, judge_client) -> dict:
    """评估事实准确性和幻觉率"""
    print("\n[2/11] 评估事实准确性...")

    all_facts = []
    for item in EXTRACTION_TEST:
        if not item["ground_truths"]:
            continue
        facts = extractor.extract(item["user"], item["assistant"])
        for fact in facts:
            all_facts.append({
                "user_input": item["user"],
                "assistant_msg": item["assistant"],
                "fact_content": fact["content"],
            })

    if not all_facts:
        return {"faithfulness": 0.0, "hallucination_rate": 1.0, "per_fact": []}

    faithful_count = 0
    hallucination_count = 0
    per_fact = []

    for item in all_facts:
        prompt = JUDGE_FACT_ACCURACY.format(
            user_input=item["user_input"],
            assistant_msg=item["assistant_msg"],
            fact=item["fact_content"],
        )
        resp = call_judge(judge_client, "你是评估专家，只输出合法JSON。", prompt)
        parsed = parse_json_safe(resp)

        is_faithful = parsed.get("faithful", False)
        is_hallucination = parsed.get("hallucination", True)

        if is_faithful:
            faithful_count += 1
        if is_hallucination:
            hallucination_count += 1

        per_fact.append({
            "fact": item["fact_content"],
            "faithful": is_faithful,
            "hallucination": is_hallucination,
            "reason": parsed.get("reason", ""),
        })

    n = len(all_facts)
    faithfulness = faithful_count / n
    hallucination_rate = hallucination_count / n

    print(f"  忠实度: {faithfulness:.2%}, 幻觉率: {hallucination_rate:.2%}")

    return {
        "faithfulness": faithfulness,
        "hallucination_rate": hallucination_rate,
        "per_fact": per_fact,
    }


# ──────────────────────────────────────────────
# 3. 重要性判断准确率评估
# ──────────────────────────────────────────────

def eval_importance_accuracy(extractor, judge_client) -> dict:
    """评估 importance 字段的判断准确率"""
    print("\n[3/11] 评估重要性判断...")

    correct = 0
    total = 0
    per_item = []

    for item in IMPORTANCE_TEST:
        facts = extractor.extract(item["input"], "好的，我记住了")
        if not facts:
            per_item.append({"id": item["id"], "expected": item["expected"], "actual": None, "correct": False})
            continue

        actual = facts[0]["importance"]
        is_correct = actual == item["expected"]
        if is_correct:
            correct += 1
        total += 1

        per_item.append({
            "id": item["id"],
            "input": item["input"],
            "expected": item["expected"],
            "actual": actual,
            "correct": is_correct,
            "reason": item["reason"],
        })
        print(f"  {item['id']} ({item['reason']}): expected={item['expected']}, actual={actual}, {'OK' if is_correct else 'WRONG'}")

    accuracy = correct / total if total > 0 else 0.0

    return {
        "accuracy": accuracy,
        "correct": correct,
        "total": total,
        "per_item": per_item,
    }


# ──────────────────────────────────────────────
# 4. 去重准确率评估
# ──────────────────────────────────────────────

def eval_dedup_accuracy(storage, writer) -> dict:
    """评估去重准确率（纯计算，无需 LLM）"""
    print("\n[4/11] 评估去重准确率...")

    # 清空
    storage.delete()

    # 写入初始记忆
    writer.write(DEDUP_TEST["initial"])
    writer._queue.join()
    time.sleep(0.5)
    initial_count = storage.count()
    print(f"  初始记忆数: {initial_count}")

    # 写入重复记忆
    writer.write(DEDUP_TEST["duplicates"])
    writer._queue.join()
    time.sleep(0.5)
    after_dup_count = storage.count()
    print(f"  写入重复后: {after_dup_count}")

    dup_discarded = initial_count == after_dup_count
    dup_false_positive = after_dup_count < initial_count

    # 写入新记忆
    new_count = len(DEDUP_TEST["new_facts"])
    writer.write(DEDUP_TEST["new_facts"])
    writer._queue.join()
    time.sleep(0.5)
    after_new_count = storage.count()
    print(f"  写入新记忆后: {after_new_count}")

    new_accepted = after_new_count - after_dup_count
    false_dedup = new_count - new_accepted

    dedup_precision = 1.0 if dup_discarded and not dup_false_positive else 0.0
    false_dedup_rate = false_dedup / new_count if new_count > 0 else 0.0

    return {
        "dedup_precision": dedup_precision,
        "false_dedup_rate": false_dedup_rate,
        "initial_count": initial_count,
        "after_dup_count": after_dup_count,
        "after_new_count": after_new_count,
        "duplicates_correctly_discarded": dup_discarded,
        "new_facts_correctly_accepted": new_accepted == new_count,
    }


# ──────────────────────────────────────────────
# 5. 冲突检测与合并质量评估
# ──────────────────────────────────────────────

JUDGE_MERGE_QUALITY = """评估记忆合并的结果质量。

原始记忆: {old_memory}
新信息: {new_memory}
合并结果: {merged_memory}

判断：
1. 合并结果是否正确反映了新信息（新信息优先）？
2. 合并结果是否保留了不冲突的旧信息？
3. 合并结果是否没有引入原文没有的信息？

输出 JSON:
{{"correct": true/false, "info_loss": true/false, "hallucination": true/false, "reason": "一句话说明"}}"""


def eval_conflict_resolution(storage, writer, judge_client) -> dict:
    """评估冲突检测和合并质量"""
    print("\n[5/11] 评估冲突处理...")

    storage.delete()
    results = []

    for item in CONFLICT_TEST:
        # 写入初始记忆
        writer.write([item["initial"]])
        writer._queue.join()
        time.sleep(0.5)
        count_before = storage.count()

        # 写入冲突记忆
        writer.write([item["conflict"]])
        writer._queue.join()
        time.sleep(2.5)  # LLM 合并需要时间
        count_after = storage.count()

        # 获取所有记忆内容
        all_data = storage.get_by_metadata(where={}, include=["documents"])
        all_contents = all_data["documents"]

        # 判断是否合并
        was_merged = count_after <= count_before

        merge_quality = None
        if was_merged and len(all_contents) > 0:
            old_content = item["initial"]["content"]
            new_content = item["conflict"]["content"]
            merged_content = all_contents[0]

            prompt = JUDGE_MERGE_QUALITY.format(
                old_memory=old_content,
                new_memory=new_content,
                merged_memory=merged_content,
            )
            resp = call_judge(judge_client, "你是评估专家，只输出合法JSON。", prompt)
            parsed = parse_json_safe(resp)
            merge_quality = parsed

        results.append({
            "id": item["id"],
            "category": item["category"],
            "initial": item["initial"]["content"],
            "conflict": item["conflict"]["content"],
            "was_merged": was_merged,
            "count_before": count_before,
            "count_after": count_after,
            "merge_quality": merge_quality,
        })
        print(f"  {item['id']} ({item['category']}): merged={was_merged}, count {count_before}→{count_after}")

    conflict_detected = sum(1 for r in results if r["was_merged"] or r["count_after"] > r["count_before"])
    conflict_detection_rate = conflict_detected / len(results) if results else 0.0

    merge_correct = 0
    merge_total = 0
    for r in results:
        if r["merge_quality"] and r["merge_quality"].get("correct") is not None:
            merge_total += 1
            if r["merge_quality"].get("correct"):
                merge_correct += 1

    merge_accuracy = merge_correct / merge_total if merge_total > 0 else None

    return {
        "conflict_detection_rate": conflict_detection_rate,
        "merge_accuracy": merge_accuracy,
        "per_item": results,
    }


# ──────────────────────────────────────────────
# 6. Core 记忆完整性评估
# ──────────────────────────────────────────────

def eval_core_integrity(storage, writer, config) -> dict:
    """评估 core 记忆写入和保护"""
    print("\n[6/11] 评估 Core 记忆完整性...")

    storage.delete()

    # 写入 high importance 记忆
    high_fact = {"content": "灰宝对花生和海鲜都过敏", "type": "semantic", "importance": "high"}
    writer.write([high_fact])
    writer._queue.join()
    time.sleep(1)

    # 检查 is_core 标记
    core_data = storage.get_by_metadata(where={"is_core": 1}, include=["documents"])
    core_write_ok = len(core_data["documents"]) > 0 and "过敏" in core_data["documents"][0]

    # 检查 core_memory.md
    core_path = Path(config.core_file)
    core_file_ok = False
    core_file_content = ""
    if core_path.exists():
        core_file_content = core_path.read_text(encoding="utf-8")
        core_file_ok = "过敏" in core_file_content

    # Core 保护：用 low importance 尝试覆盖
    low_override = {"content": "灰宝其实不过敏了", "type": "semantic", "importance": "low"}
    writer.write([low_override])
    writer._queue.join()
    time.sleep(1)

    core_after = storage.get_by_metadata(where={"is_core": 1}, include=["documents"])
    core_protected = any("过敏" in doc for doc in core_after["documents"])

    print(f"  Core 写入: {core_write_ok}, Core 文件: {core_file_ok}, Core 保护: {core_protected}")

    return {
        "core_write_rate": 1.0 if core_write_ok else 0.0,
        "core_file_generated": core_file_ok,
        "core_protection_rate": 1.0 if core_protected else 0.0,
    }


# ──────────────────────────────────────────────
# 7. PII 过滤率评估
# ──────────────────────────────────────────────

def eval_pii_filter(extractor) -> dict:
    """评估 PII 过滤效果"""
    print("\n[7/11] 评估 PII 过滤...")

    filtered_count = 0
    total_pii = 0
    per_item = []

    for item in PII_TEST:
        facts = extractor.extract(item["user"], item["assistant"])
        extracted_texts = [f["content"] for f in facts] if facts else []

        if item["contains_pii"]:
            total_pii += 1
            has_pii_in_result = any(
                any(p in t for p in ["138", "abc123", "110101"])
                for t in extracted_texts
            )
            if not has_pii_in_result:
                filtered_count += 1
            per_item.append({
                "id": item["id"],
                "input": item["user"],
                "contains_pii": True,
                "filtered": not has_pii_in_result,
                "extracted": extracted_texts,
            })
            print(f"  {item['id']}: filtered={not has_pii_in_result}, extracted={extracted_texts}")
        else:
            per_item.append({
                "id": item["id"],
                "input": item["user"],
                "contains_pii": False,
                "filtered": True,
                "extracted": extracted_texts,
            })

    filter_rate = filtered_count / total_pii if total_pii > 0 else 1.0

    return {
        "filter_rate": filter_rate,
        "filtered": filtered_count,
        "total_pii": total_pii,
        "per_item": per_item,
    }


# ──────────────────────────────────────────────
# 8. 写入延迟评估
# ──────────────────────────────────────────────

def eval_write_latency(storage, writer) -> dict:
    """评估写入延迟"""
    print("\n[8/11] 评估写入延迟...")

    storage.delete()
    latencies = []

    test_facts = [
        {"content": f"测试记忆{i}", "type": "semantic", "importance": "low"}
        for i in range(5)
    ]

    for fact in test_facts:
        start = time.time()
        writer.write([fact])
        writer._queue.join()
        time.sleep(0.3)
        elapsed = time.time() - start
        latencies.append(elapsed)

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]

    print(f"  P50: {p50*1000:.0f}ms, P95: {p95*1000:.0f}ms")

    return {
        "p50_ms": p50 * 1000,
        "p95_ms": p95 * 1000,
        "per_fact_ms": [l * 1000 for l in latencies],
    }


# ──────────────────────────────────────────────
# 9. 检索质量评估（Recall@K / Hit Rate / MRR）
# ──────────────────────────────────────────────

def is_relevant(doc_content: str, relevant_contents: list[str]) -> bool:
    if not relevant_contents:
        return False
    for rc in relevant_contents:
        if rc in doc_content:
            return True
    return False


def eval_retrieval_metrics(retriever, storage, config) -> dict:
    """评估检索质量（客观指标，参考 RAG Phase1 评估）"""
    print("\n[9/11] 评估检索质量 (Recall@K / Hit Rate / MRR)...")

    # 写入初始记忆
    storage.delete()
    for mem in RETRIEVAL_INITIAL_MEMORIES:
        storage.add(mem["id"], mem["content"], mem["is_core"], mem["memory_type"])

    # 生成 core_memory.md（检索器依赖此文件读取 core 记忆）
    core_memories = [m for m in RETRIEVAL_INITIAL_MEMORIES if m["is_core"]]
    if core_memories:
        core_path = Path(config.core_file)
        core_path.parent.mkdir(parents=True, exist_ok=True)
        core_text = "# 灰宝的档案\n" + "\n".join(f"- {m['content']}" for m in core_memories)
        core_path.write_text(core_text, encoding="utf-8")

    per_query = []

    for item in RETRIEVAL_TEST:
        result_text = retriever.retrieve(item["query"])

        # 检查每个相关内容是否出现在结果中
        relevant_found = []
        for rc in item["relevant_contents"]:
            if rc in result_text:
                relevant_found.append(rc)

        total_relevant = len(item["relevant_contents"])
        if total_relevant == 0:
            # 无关查询，检查是否返回了噪音
            hit = 0
            recall = 1.0
        else:
            hit = 1 if relevant_found else 0
            recall = len(relevant_found) / total_relevant

        per_query.append({
            "id": item["id"],
            "query": item["query"],
            "category": item["category"],
            "total_relevant": total_relevant,
            "relevant_found": len(relevant_found),
            "hit": hit,
            "recall": recall,
        })
        print(f"  {item['id']} ({item['category']}): hit={hit}, recall={recall:.2f}")

    n = len(per_query)
    hit_rate = sum(q["hit"] for q in per_query) / n
    avg_recall = sum(q["recall"] for q in per_query) / n

    # 计算有效查询（有 ground truth 的）的 MRR
    valid_queries = [q for q in per_query if q["total_relevant"] > 0]
    # MRR 简化：hit 的第一条排名假设为1（因为我们只能判断命中与否，无法精确排名）
    mrr = sum(1.0 for q in valid_queries if q["hit"] == 1) / len(valid_queries) if valid_queries else 0.0

    return {
        "hit_rate": hit_rate,
        "recall_at_k": avg_recall,
        "mrr": mrr,
        "per_query": per_query,
    }


# ──────────────────────────────────────────────
# 10. 检索延迟评估
# ──────────────────────────────────────────────

def eval_retrieval_latency(retriever) -> dict:
    """评估检索延迟"""
    print("\n[10/11] 评估检索延迟...")

    queries = ["灰宝过敏什么", "灰宝在学什么", "灰宝喜欢什么", "灰宝工作在哪", "你还记得关于我的一切吗"]
    latencies = []

    for query in queries:
        start = time.time()
        retriever.retrieve(query)
        elapsed = time.time() - start
        latencies.append(elapsed)

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]

    print(f"  P50: {p50*1000:.0f}ms, P95: {p95*1000:.0f}ms")

    return {
        "p50_ms": p50 * 1000,
        "p95_ms": p95 * 1000,
        "per_query_ms": [l * 1000 for l in latencies],
    }


# ──────────────────────────────────────────────
# 11. 答案相关性评估（LLM-as-judge）
# ──────────────────────────────────────────────

JUDGE_RELEVANCE = """评估检索到的记忆与用户查询的相关性。

用户查询: {query}
检索到的记忆:
{retrieved_memories}

对每条检索结果评估：
- relevant: 与查询直接相关，能帮助回答问题
- partially_relevant: 有一定关联但不是核心信息
- irrelevant: 与查询无关

输出 JSON:
{{"memories": [{{"memory": "记忆内容摘要", "relevance": "relevant/partially_relevant/irrelevant"}}], "overall_relevance": "high/medium/low"}}"""


def eval_answer_relevance(retriever, storage, config, judge_client) -> dict:
    """评估检索结果的相关性（LLM-as-judge）"""
    print("\n[11/11] 评估答案相关性...")

    # 确保记忆已写入
    if storage.count() == 0:
        for mem in RETRIEVAL_INITIAL_MEMORIES:
            storage.add(mem["id"], mem["content"], mem["is_core"], mem["memory_type"])

    # 确保 core_memory.md 存在
    core_memories = [m for m in RETRIEVAL_INITIAL_MEMORIES if m["is_core"]]
    core_path = Path(config.core_file)
    if core_memories and not core_path.exists():
        core_path.parent.mkdir(parents=True, exist_ok=True)
        core_text = "# 灰宝的档案\n" + "\n".join(f"- {m['content']}" for m in core_memories)
        core_path.write_text(core_text, encoding="utf-8")

    per_query = []

    for item in RETRIEVAL_TEST:
        if not item["relevant_contents"]:
            continue

        result_text = retriever.retrieve(item["query"])

        if not result_text:
            per_query.append({
                "id": item["id"],
                "query": item["query"],
                "relevance": "none",
                "score": 0.0,
            })
            continue

        prompt = JUDGE_RELEVANCE.format(
            query=item["query"],
            retrieved_memories=result_text,
        )
        resp = call_judge(judge_client, "你是评估专家，只输出合法JSON。", prompt)
        parsed = parse_json_safe(resp)

        overall = parsed.get("overall_relevance", "low")
        score_map = {"high": 1.0, "medium": 0.6, "low": 0.2, "none": 0.0}
        score = score_map.get(overall, 0.0)

        per_query.append({
            "id": item["id"],
            "query": item["query"],
            "relevance": overall,
            "score": score,
        })
        print(f"  {item['id']} ({item['category']}): relevance={overall}, score={score:.1f}")

    n = len(per_query)
    avg_score = sum(q["score"] for q in per_query) / n if n > 0 else 0.0

    return {
        "average_relevance": avg_score,
        "per_query": per_query,
    }


# ──────────────────────────────────────────────
# 报告生成
# ──────────────────────────────────────────────

def generate_report(results: dict) -> str:
    """生成完整 Markdown 评估报告"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# 记忆系统评估报告",
        "",
        f"**评估时间**：{ts}",
        f"**主模型**：{os.getenv('FENGJIN_MODEL', 'N/A')}",
        f"**心智模型**：{os.getenv('MIND_MODEL', 'N/A')}",
        "",
        "---",
        "",
        "## 评估环境",
        "",
        f"- ChromaDB 持久化目录: `{TEST_CHROMA_DIR}`",
        f"- Collection: `{TEST_COLLECTION}`",
        f"- dedup_distance: {results.get('config', {}).get('dedup_distance', 'N/A')}",
        f"- conflict_distance: {results.get('config', {}).get('conflict_distance', 'N/A')}",
        f"- top_k: {results.get('config', {}).get('top_k', 'N/A')}",
        "",
        "---",
        "",
        "## 写入阶段评估",
        "",
        "| 指标 | 得分 | 说明 |",
        "|------|------|------|",
    ]

    ext = results.get("extraction_coverage", {})
    lines.append(f"| 提取覆盖率 (GT Recall) | {ext.get('gt_recall', 0):.1%} | match + partial / total |")
    lines.append(f"| 严格覆盖率 (Strict Recall) | {ext.get('strict_recall', 0):.1%} | 仅 match / total |")

    fa = results.get("fact_accuracy", {})
    lines.append(f"| 事实准确性 (Faithfulness) | {fa.get('faithfulness', 0):.1%} | 忠实于对话的事实比例 |")
    lines.append(f"| 幻觉率 (Hallucination Rate) | {fa.get('hallucination_rate', 0):.1%} | 包含幻觉的事实比例 |")

    imp = results.get("importance_accuracy", {})
    lines.append(f"| 重要性判断准确率 | {imp.get('accuracy', 0):.1%} | {imp.get('correct', 0)}/{imp.get('total', 0)} 正确 |")

    dedup = results.get("dedup_accuracy", {})
    lines.append(f"| 去重准确率 | {dedup.get('dedup_precision', 0):.0%} | 重复记忆是否被正确丢弃 |")
    lines.append(f"| 误去重率 | {dedup.get('false_dedup_rate', 0):.0%} | 新记忆被错误丢弃的比例 |")

    conflict = results.get("conflict_resolution", {})
    lines.append(f"| 冲突检测率 | {conflict.get('conflict_detection_rate', 0):.0%} | 矛盾信息被检测到的比例 |")
    merge_acc = conflict.get("merge_accuracy")
    if merge_acc is not None:
        lines.append(f"| 合并质量 (Merge Accuracy) | {merge_acc:.0%} | 合并结果是否正确 |")
    else:
        lines.append(f"| 合并质量 (Merge Accuracy) | N/A | 未能评估（未触发合并） |")

    core = results.get("core_integrity", {})
    lines.append(f"| Core 写入率 | {core.get('core_write_rate', 0):.0%} | high importance 是否写入 core |")
    lines.append(f"| Core 文件生成 | {'是' if core.get('core_file_generated') else '否'} | core_memory.md 是否生成 |")
    lines.append(f"| Core 保护率 | {core.get('core_protection_rate', 0):.0%} | low 是否无法覆盖 core |")

    pii = results.get("pii_filter", {})
    lines.append(f"| PII 过滤率 | {pii.get('filter_rate', 0):.0%} | {pii.get('filtered', 0)}/{pii.get('total_pii', 0)} 被过滤 |")

    wl = results.get("write_latency", {})
    lines.append(f"| 写入延迟 P50 | {wl.get('p50_ms', 0):.0f} ms | |")
    lines.append(f"| 写入延迟 P95 | {wl.get('p95_ms', 0):.0f} ms | |")

    lines += [
        "",
        "---",
        "",
        "## 检索阶段评估",
        "",
        "| 指标 | 得分 | 说明 |",
        "|------|------|------|",
    ]

    ret = results.get("retrieval_metrics", {})
    lines.append(f"| Recall@K | {ret.get('recall_at_k', 0):.1%} | 检索到的相关记忆比例 |")
    lines.append(f"| Hit Rate | {ret.get('hit_rate', 0):.1%} | 至少命中一条相关记忆的查询比例 |")
    lines.append(f"| MRR | {ret.get('mrr', 0):.2f} | 第一条相关记忆的平均排名倒数 |")

    rl = results.get("retrieval_latency", {})
    lines.append(f"| 检索延迟 P50 | {rl.get('p50_ms', 0):.0f} ms | |")
    lines.append(f"| 检索延迟 P95 | {rl.get('p95_ms', 0):.0f} ms | |")

    ar = results.get("answer_relevance", {})
    lines.append(f"| 答案相关性 | {ar.get('average_relevance', 0):.2f} | LLM judge 评估（0-1） |")

    # ── 发现的问题 ──
    lines += ["", "---", "", "## 发现的问题", ""]
    issues = []

    if ext.get("gt_recall", 1) < 0.6:
        issues.append("提取覆盖率偏低（<60%），小模型可能遗漏重要事实")
    if fa.get("hallucination_rate", 0) > 0.3:
        issues.append("幻觉率偏高（>30%），提取 prompt 需要优化")
    if imp.get("accuracy", 1) < 0.7:
        issues.append("重要性判断准确率偏低（<70%），过敏/禁忌等信息可能未被标记为 high")
    if not dedup.get("duplicates_correctly_discarded"):
        issues.append("去重未生效，语义重复的记忆未被识别")
    if dedup.get("false_dedup_rate", 0) > 0:
        issues.append("误去重率 > 0，新记忆被错误丢弃")
    if not core.get("core_write_rate"):
        issues.append("Core 记忆写入失败，high importance 记忆未获得 is_core 标记")
    if not core.get("core_protection_rate"):
        issues.append("Core 保护失效，low importance 覆盖了 core 记忆")
    if pii.get("filter_rate", 1) < 0.8:
        issues.append("PII 过滤不完善，手机号/密码等敏感信息未被拦截")
    if ret.get("hit_rate", 1) < 0.7:
        issues.append("检索命中率偏低（<70%），相关记忆未能被检索到")
    if rl.get("p50_ms", 0) > 500:
        issues.append("检索延迟偏高（P50 > 500ms），影响用户体验")

    if not issues:
        lines.append("所有指标均在正常范围内，未发现明显问题。")
    else:
        for i, issue in enumerate(issues, 1):
            lines.append(f"{i}. {issue}")

    # ── 改进建议 ──
    lines += ["", "---", "", "## 改进建议", ""]
    suggestions = []

    if ext.get("gt_recall", 1) < 0.6:
        suggestions.append("优化提取 prompt：增加更多 few-shot 示例，明确提取标准")
    if fa.get("hallucination_rate", 0) > 0.3:
        suggestions.append("强化忠实度约束：在 prompt 中强调'只能提取对话中明确提到的信息'")
    if imp.get("accuracy", 1) < 0.7:
        suggestions.append("细化重要性判断规则：在 prompt 中列举 must-high 场景（过敏、疾病、显式要求）")
    if not dedup.get("duplicates_correctly_discarded"):
        suggestions.append("调整 dedup_distance 阈值或改用更好的 embedding 模型")
    if not core.get("core_protection_rate"):
        suggestions.append("检查 writer._resolve_conflict 中 core 保护逻辑")
    if pii.get("filter_rate", 1) < 0.8:
        suggestions.append("扩展 blacklist_patterns，覆盖更多 PII 模式")
    if ret.get("hit_rate", 1) < 0.7:
        suggestions.append("优化检索：考虑增大 top_k 或换用中文优化的 embedding 模型")
    if rl.get("p50_ms", 0) > 500:
        suggestions.append("优化检索延迟：考虑缓存 core 文件、减少 ChromaDB 查询开销")

    # 通用建议
    suggestions.append("考虑使用中文优化的 embedding 模型替代默认的 all-MiniLM-L6-v2（英文模型，中文距离偏高）")

    if not suggestions or (len(suggestions) == 1 and "embedding" in suggestions[0]):
        lines.append("系统表现良好。通用优化方向：")
    else:
        lines.append("按优先级排序：")

    for i, sug in enumerate(suggestions, 1):
        lines.append(f"{i}. {sug}")

    # ── 逐条详情（写入阶段） ──
    lines += ["", "---", "", "## 写入阶段逐条详情", ""]
    for item in ext.get("per_item", []):
        lines.append(f"### {item['id']} ({item['category']})")
        lines.append(f"- 对话: {item.get('ground_truths', [])}")
        lines.append(f"- 提取: {item.get('extracted', [])}")
        lines.append(f"- GT Recall: {item.get('gt_recall', 0):.2f}, Strict: {item.get('strict_recall', 0):.2f}")
        lines.append("")

    # ── 逐条详情（检索阶段） ──
    lines += ["", "## 检索阶段逐条详情", ""]
    for item in ret.get("per_query", []):
        lines.append(f"### {item['id']} ({item['category']})")
        lines.append(f"- 查询: {item['query']}")
        lines.append(f"- 命中: {item['hit']}, Recall: {item.get('recall', 0):.2f}")
        lines.append("")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────

def main():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # 环境检查
    print("=" * 50)
    print("记忆系统完整评估")
    print("=" * 50)
    print("\n环境检查...")
    for key in ["FENGJIN_API_KEY", "FENGJIN_BASE_URL", "FENGJIN_MODEL",
                "MIND_API_KEY", "MIND_BASE_URL", "MIND_MODEL"]:
        val = os.getenv(key)
        if not val:
            print(f"  错误: 缺少环境变量 {key}")
            sys.exit(1)
    print("  环境检查通过\n")

    # 初始化组件：从 memory.yaml 加载完整配置，覆盖 chroma 路径为隔离测试目录
    print("初始化组件（隔离测试环境）...")
    settings = MemorySettings.load()
    config = settings.memory
    config.chroma.persist_directory = TEST_CHROMA_DIR
    config.chroma.collection_name = TEST_COLLECTION

    storage = MemoryStorage(config)
    storage.delete()  # 清空旧测试数据
    print(f"  ChromaDB: {TEST_CHROMA_DIR}/{TEST_COLLECTION} (count={storage.count()})")

    mind_client = MemorySettings.create_mind_model_client()
    mind_model = MemorySettings.get_mind_model_name()
    print(f"  心智模型: {mind_model}")

    extractor = MemoryExtractor(config, mind_client, mind_model, storage)
    writer = MemoryWriter(config, mind_client, mind_model, storage)
    retriever = MemoryRetriever(config, storage)

    judge_client = get_judge_client()
    print("  LLM Judge 初始化完成")

    # 开始评估
    results = {
        "config": {
            "dedup_distance": config.thresholds.dedup_distance,
            "conflict_distance": config.thresholds.conflict_distance,
            "top_k": config.retrieval.top_k,
        }
    }

    try:
        # 写入阶段
        print("\n" + "=" * 50)
        print("写入阶段评估")
        print("=" * 50)

        results["extraction_coverage"] = eval_extraction_coverage(extractor, judge_client)
        storage.delete()  # 每个子测试前清空

        results["fact_accuracy"] = eval_fact_accuracy(extractor, judge_client)
        storage.delete()

        results["importance_accuracy"] = eval_importance_accuracy(extractor, judge_client)
        storage.delete()

        results["dedup_accuracy"] = eval_dedup_accuracy(storage, writer)
        storage.delete()

        results["conflict_resolution"] = eval_conflict_resolution(storage, writer, judge_client)
        storage.delete()

        results["core_integrity"] = eval_core_integrity(storage, writer, config)
        storage.delete()

        results["pii_filter"] = eval_pii_filter(extractor)
        storage.delete()

        results["write_latency"] = eval_write_latency(storage, writer)
        storage.delete()

        # 检索阶段
        print("\n" + "=" * 50)
        print("检索阶段评估")
        print("=" * 50)

        results["retrieval_metrics"] = eval_retrieval_metrics(retriever, storage, config)

        results["retrieval_latency"] = eval_retrieval_latency(retriever)

        results["answer_relevance"] = eval_answer_relevance(retriever, storage, config, judge_client)

    finally:
        # 清理
        writer.stop()
        storage.delete()
        print("\n测试数据已清理")

    # 生成报告
    report = generate_report(results)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORTS_DIR / f"memory_eval_{timestamp}.md"
    report_path.write_text(report, encoding="utf-8")

    # 控制台输出摘要
    print("\n" + "=" * 50)
    print("评估完成！摘要")
    print("=" * 50)
    print(f"  提取覆盖率 (GT Recall): {results['extraction_coverage']['gt_recall']:.1%}")
    print(f"  事实准确性: {results['fact_accuracy']['faithfulness']:.1%}")
    print(f"  幻觉率: {results['fact_accuracy']['hallucination_rate']:.1%}")
    print(f"  重要性准确率: {results['importance_accuracy']['accuracy']:.1%}")
    print(f"  去重准确率: {results['dedup_accuracy']['dedup_precision']:.0%}")
    print(f"  Core 写入率: {results['core_integrity']['core_write_rate']:.0%}")
    print(f"  Core 保护率: {results['core_integrity']['core_protection_rate']:.0%}")
    print(f"  PII 过滤率: {results['pii_filter']['filter_rate']:.0%}")
    print(f"  检索 Hit Rate: {results['retrieval_metrics']['hit_rate']:.1%}")
    print(f"  检索 Recall@K: {results['retrieval_metrics']['recall_at_k']:.1%}")
    print(f"  检索 MRR: {results['retrieval_metrics']['mrr']:.2f}")
    print(f"  检索延迟 P50: {results['retrieval_latency']['p50_ms']:.0f}ms")
    print(f"  答案相关性: {results['answer_relevance']['average_relevance']:.2f}")
    print(f"\n完整报告: {report_path}")


if __name__ == "__main__":
    main()
