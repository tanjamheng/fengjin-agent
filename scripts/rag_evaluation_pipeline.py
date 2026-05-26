"""
RAG评估一条龙脚本
=================

使用方法：
1. 配置你的RAG系统入口（query函数）
2. 准备测试问题列表
3. 运行脚本，自动输出所有客观指标 + 问题诊断

依赖安装：
pip install ragas datasets

配置LLM（用于评估）：
- 默认使用OpenAI，需设置OPENAI_API_KEY
- 可切换为本地Ollama或DeepSeek（见配置部分）
"""

import json
import pandas as pd
from datetime import datetime
from typing import List, Dict, Callable, Any
from dataclasses import dataclass

# RAGAS评估指标
from ragas import evaluate
from ragas.metrics import (
    context_precision,   # Context Precision（检索精确率）
    context_recall,      # Context Recall（检索召回率）
    faithfulness,        # Groundedness/Faithfulness（忠实度）
    answer_relevancy,    # Answer Relevance（答案相关性）
)
from ragas.metrics._context_precision import ContextPrecision
from ragas.metrics._context_recall import ContextRecall
from datasets import Dataset

# ============================================================================
# 配置部分
# ============================================================================

EVALUATION_CONFIG = {
    # 阈值设定（业界标准）
    "thresholds": {
        "context_relevance": 0.30,   # Phase 1最低门槛
        "context_precision": 0.50,   # 生产标准
        "context_recall": 0.60,      # Phase 2标准
        "groundedness": 0.70,        # Phase 1最低门槛，生产要求0.85
        "answer_relevance": 0.60,    # Phase 1最低门槛
    },

    # LLM配置（用于RAGAS评估）
    "llm": {
        "provider": "openai",        # 可选：openai, ollama, deepseek
        "model": "gpt-4o-mini",      # 评估模型
        # "provider": "ollama",
        # "model": "qwen2.5:7b",
        # "provider": "deepseek",
        # "model": "deepseek-chat",
        # "api_base": "https://api.deepseek.com/v1",
    },

    # 输出配置
    "output": {
        "save_report": True,
        "report_path": "rag_evaluation_report.json",
        "save_csv": True,
        "csv_path": "rag_evaluation_details.csv",
    }
}


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class RAGResponse:
    """RAG系统返回的数据结构"""
    answer: str                    # 生成的回答
    contexts: List[str]            # 检索到的文档列表
    retrieval_scores: List[float]  # 各文档的相似度分数（可选）

@dataclass
class EvaluationResult:
    """评估结果"""
    context_precision: float
    context_recall: float
    groundedness: float
    answer_relevance: float
    diagnosis: str                  # 问题诊断结果
    passed: bool                    # 是否通过Phase 1


# ============================================================================
# RAG系统接口封装
# ============================================================================

class RAGSystemWrapper:
    """
    RAG系统封装器

    用户需要实现 get_response 方法，或直接传入已有的query函数
    """

    def __init__(self, query_func: Callable[[str], Any] = None):
        """
        初始化

        Args:
            query_func: 你的RAG系统的query函数
                        输入：问题字符串
                        输出：需包含answer和contexts
        """
        self.query_func = query_func

    def get_response(self, question: str) -> RAGResponse:
        """
        获取RAG响应

        用户需要根据自己RAG系统的实际返回格式来适配这个方法
        """
        if self.query_func is None:
            raise NotImplementedError("请实现 get_response 方法或传入 query_func")

        # 调用RAG系统
        raw_response = self.query_func(question)

        # 适配不同的RAG框架返回格式
        # 方式1：LlamaIndex格式
        if hasattr(raw_response, 'response') and hasattr(raw_response, 'source_nodes'):
            answer = raw_response.response
            contexts = [node.node.text for node in raw_response.source_nodes]
            retrieval_scores = [node.score for node in raw_response.source_nodes]

        # 方式2：LangChain格式
        elif isinstance(raw_response, dict) and 'answer' in raw_response:
            answer = raw_response['answer']
            contexts = raw_response.get('contexts', [])
            retrieval_scores = raw_response.get('scores', [])

        # 方式3：自定义格式（直接返回answer和contexts）
        elif isinstance(raw_response, tuple) and len(raw_response) == 2:
            answer, contexts = raw_response
            retrieval_scores = []

        # 方式4：只有字符串answer（无contexts）
        elif isinstance(raw_response, str):
            answer = raw_response
            contexts = []
            retrieval_scores = []

        else:
            raise ValueError(f"无法识别RAG返回格式: {type(raw_response)}")

        return RAGResponse(
            answer=answer,
            contexts=contexts,
            retrieval_scores=retrieval_scores
        )


# ============================================================================
# 一条龙评估器
# ============================================================================

class RAGEvaluator:
    """
    RAG一条龙评估器

    功能：
    1. 调用RAG系统获取回答和检索文档
    2. 运行RAGAS全指标评估
    3. 自动诊断问题来源（检索 vs 生成）
    4. 输出详细报告
    """

    def __init__(
        self,
        rag_system: RAGSystemWrapper,
        config: Dict = EVALUATION_CONFIG
    ):
        self.rag_system = rag_system
        self.config = config
        self._setup_llm()

    def _setup_llm(self):
        """配置评估用的LLM"""
        llm_config = self.config["llm"]
        provider = llm_config["provider"]

        if provider == "openai":
            # RAGAS默认使用OpenAI，无需额外配置
            # 确保环境变量 OPENAI_API_KEY 已设置
            pass

        elif provider == "ollama":
            # 配置本地Ollama
            from ragas.llms import LangchainLLMWrapper
            from langchain_ollama import ChatOllama

            local_llm = ChatOllama(model=llm_config["model"])
            # 设置为全局评估LLM（RAGAS会使用）
            import ragas
            ragas.llm = LangchainLLMWrapper(local_llm)

        elif provider == "deepseek":
            # 配置DeepSeek API
            from ragas.llms import LangchainLLMWrapper
            from langchain_openai import ChatOpenAI

            deepseek_llm = ChatOpenAI(
                model=llm_config["model"],
                openai_api_base=llm_config["api_base"],
                openai_api_key=os.environ.get("DEEPSEEK_API_KEY"),
            )
            import ragas
            ragas.llm = LangchainLLMWrapper(deepseek_llm)

    def run_evaluation(
        self,
        questions: List[str],
        ground_truths: List[str] = None,
        ground_truth_contexts: List[List[str]] = None,
    ) -> EvaluationResult:
        """
        运行完整评估

        Args:
            questions: 测试问题列表
            ground_truths: 标准答案列表（可选，用于Context Recall）
            ground_truth_contexts: 标准上下文列表（可选，用于Context Recall）

        Returns:
            EvaluationResult: 包含所有指标和诊断结果
        """
        print("=" * 60)
        print("RAG评估一条龙 - 开始")
        print("=" * 60)

        # Step 1: 调用RAG系统获取响应
        print("\n[Step 1] 调用RAG系统获取响应...")
        answers = []
        contexts_list = []

        for i, question in enumerate(questions):
            print(f"  处理问题 {i+1}/{len(questions)}: {question[:50]}...")
            response = self.rag_system.get_response(question)
            answers.append(response.answer)
            contexts_list.append(response.contexts)

        print(f"  完成！共处理 {len(questions)} 个问题")

        # Step 2: 准备评估数据集
        print("\n[Step 2] 准备评估数据集...")
        eval_data = {
            "question": questions,
            "answer": answers,
            "contexts": contexts_list,
        }

        # 如果有标准答案，添加ground_truth
        if ground_truths:
            eval_data["ground_truth"] = ground_truths

        dataset = Dataset.from_dict(eval_data)

        # Step 3: 运行RAGAS评估
        print("\n[Step 3] 运行RAGAS评估（全部核心指标）...")
        metrics = [
            context_precision,
            faithfulness,
            answer_relevancy,
        ]

        # Context Recall需要ground_truth
        if ground_truths:
            metrics.append(context_recall)
            print("  - 包含 Context Recall（有标准答案）")
        else:
            print("  - 跳过 Context Recall（无标准答案）")

        print("  评估中...")
        results = evaluate(dataset, metrics=metrics)

        # Step 4: 提取指标分数
        print("\n[Step 4] 提取评估指标...")
        result_df = results.to_pandas()

        scores = {
            "context_precision": float(result_df['context_precision'].mean()),
            "groundedness": float(result_df['faithfulness'].mean()),
            "answer_relevance": float(result_df['answer_relevancy'].mean()),
        }

        if ground_truths:
            scores["context_recall"] = float(result_df['context_recall'].mean())
        else:
            scores["context_recall"] = None

        # Step 5: 诊断问题来源
        print("\n[Step 5] 诊断问题来源...")
        diagnosis = self._diagnose(scores)

        # Step 6: 判断是否通过Phase 1
        passed = self._check_phase1_passed(scores)

        # Step 7: 输出报告
        print("\n[Step 6] 生成报告...")
        self._print_summary(scores, diagnosis, passed)

        if self.config["output"]["save_report"]:
            self._save_report(scores, diagnosis, passed, questions, answers, contexts_list)

        if self.config["output"]["save_csv"]:
            result_df.to_csv(self.config["output"]["csv_path"], index=False)
            print(f"  详细报告已保存: {self.config['output']['csv_path']}")

        return EvaluationResult(
            context_precision=scores["context_precision"],
            context_recall=scores["context_recall"] or 0.0,
            groundedness=scores["groundedness"],
            answer_relevance=scores["answer_relevance"],
            diagnosis=diagnosis,
            passed=passed
        )

    def _diagnose(self, scores: Dict[str, float]) -> str:
        """
        根据指标诊断问题来源

        使用RAG Triad诊断逻辑
        """
        thresholds = self.config["thresholds"]

        context_prec = scores["context_precision"]
        context_recall = scores.get("context_recall", 0.5)  # 无数据时假设中等
        groundedness = scores["groundedness"]
        answer_rel = scores["answer_relevance"]

        diagnosis_parts = []

        # 检索问题诊断
        if context_prec < thresholds["context_relevance"]:
            diagnosis_parts.append("⚠️ 检索问题：Context Precision过低，检索噪声多或检索失败")
            if context_prec < 0.15:
                diagnosis_parts.append("   → 严重：几乎没检索到相关文档，检查Embedding和检索逻辑")
            else:
                diagnosis_parts.append("   → 建议：降低Top-K、添加Reranker、设置相似度阈值")

        if context_recall is not None and context_recall < thresholds["context_recall"]:
            diagnosis_parts.append("⚠️ 检索问题：Context Recall过低，关键信息遗漏")
            diagnosis_parts.append("   → 建议：增加Top-K、优化分块策略、改进Embedding")

        # 生成问题诊断
        if groundedness < thresholds["groundedness"]:
            if context_prec >= thresholds["context_relevance"]:
                diagnosis_parts.append("⚠️ 生成问题：Groundedness低但检索正常 → LLM在胡编")
                diagnosis_parts.append("   → 建议：优化Prompt强调引用来源、换更强LLM")
            else:
                diagnosis_parts.append("⚠️ 混合问题：检索和生成都有问题")
                diagnosis_parts.append("   → 建议：先修检索，再修生成")

        # 答案相关性诊断
        if answer_rel < thresholds["answer_relevance"]:
            if groundedness >= thresholds["groundedness"]:
                diagnosis_parts.append("⚠️ Prompt问题：Groundedness正常但Answer Relevance低 → 答非所问")
                diagnosis_parts.append("   → 建议：优化Prompt明确回答意图")
            else:
                diagnosis_parts.append("⚠️ 综合问题：幻觉+不切题，优先解决幻觉")

        # 全达标
        if not diagnosis_parts:
            diagnosis_parts.append("✅ 系统健康：所有指标达标")
            diagnosis_parts.append("   → 建议：进入Phase 3生产把关阶段")

        return "\n".join(diagnosis_parts)

    def _check_phase1_passed(self, scores: Dict[str, float]) -> bool:
        """检查是否通过Phase 1"""
        thresholds = self.config["thresholds"]

        return (
            scores["context_precision"] >= thresholds["context_relevance"] and
            scores["groundedness"] >= thresholds["groundedness"] and
            scores["answer_relevance"] >= thresholds["answer_relevance"]
        )

    def _print_summary(self, scores: Dict, diagnosis: str, passed: bool):
        """打印评估摘要"""
        print("\n" + "=" * 60)
        print("评估结果摘要")
        print("=" * 60)

        thresholds = self.config["thresholds"]

        print("\n【核心指标】")
        for metric, score in scores.items():
            if score is None:
                continue
            threshold = thresholds.get(metric, 0.5)
            status = "✅ 达标" if score >= threshold else "❌ 不达标"
            print(f"  {metric:20s}: {score:.3f} (阈值: {threshold:.2f}) {status}")

        print("\n【诊断结果】")
        print(diagnosis)

        print("\n【Phase 1判定】")
        if passed:
            print("  ✅ 通过Phase 1，可进入Phase 2系统优化")
        else:
            print("  ❌ 未通过Phase 1，需先修复上述问题")

        print("=" * 60)

    def _save_report(self, scores, diagnosis, passed, questions, answers, contexts):
        """保存完整报告"""
        report = {
            "evaluation_date": datetime.now().isoformat(),
            "metrics": scores,
            "diagnosis": diagnosis,
            "phase1_passed": passed,
            "config": self.config,
            "test_cases": [
                {
                    "question": q,
                    "answer": a,
                    "contexts": c,
                }
                for q, a, c in zip(questions, answers, contexts)
            ]
        }

        with open(self.config["output"]["report_path"], "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        print(f"  完整报告已保存: {self.config['output']['report_path']}")


# ============================================================================
# 快速使用示例
# ============================================================================

def quick_evaluate(
    query_func: Callable,
    questions: List[str],
    ground_truths: List[str] = None,
) -> EvaluationResult:
    """
    快速评估函数 - 一行代码完成评估

    Args:
        query_func: 你的RAG系统query函数
        questions: 测试问题列表
        ground_truths: 标准答案列表（可选）

    Returns:
        EvaluationResult: 评估结果

    Example:
        # 假设你有一个RAG系统
        def my_rag_query(question):
            # 你的RAG实现
            return {"answer": "...", "contexts": ["..."]}

        # 一行调用评估
        result = quick_evaluate(
            query_func=my_rag_query,
            questions=["什么是RAG?", "如何评估RAG系统?"]
        )
    """
    rag_system = RAGSystemWrapper(query_func)
    evaluator = RAGEvaluator(rag_system)
    return evaluator.run_evaluation(questions, ground_truths)


# ============================================================================
# 主程序入口
# ============================================================================

def main():
    """
    主程序示例

    演示如何使用评估脚本
    """

    # -------------------------------------------------
    # 示例1：模拟一个RAG系统
    # -------------------------------------------------
    def mock_rag_query(question: str):
        """
        模拟RAG系统（用于演示）

        实际使用时，替换为你的真实RAG系统query函数
        """
        # 模拟检索
        mock_contexts = [
            "RAG是检索增强生成技术，结合检索和生成提高回答准确性。",
            "评估RAG需要使用RAG Triad指标：Context Relevance, Groundedness, Answer Relevance。",
        ]

        # 模拟生成
        mock_answer = f"根据检索到的信息，{question}的答案是..."

        # 返回格式（适配LlamaIndex风格）
        return {
            "answer": mock_answer,
            "contexts": mock_contexts,
        }

    # -------------------------------------------------
    # 测试数据
    # -------------------------------------------------
    test_questions = [
        "什么是RAG技术？",
        "如何评估RAG系统？",
        "RAG Triad包含哪些指标？",
    ]

    # 标准答案（可选，用于Context Recall）
    test_ground_truths = [
        "RAG是检索增强生成技术，通过检索外部知识增强LLM回答。",
        "使用RAG Triad框架评估：Context Relevance、Groundedness、Answer Relevance。",
        "RAG Triad包含Context Relevance、Groundedness、Answer Relevance三个指标。",
    ]

    # -------------------------------------------------
    # 运行评估
    # -------------------------------------------------
    print("\n使用示例：")
    print("-" * 60)

    # 方式1：使用quick_evaluate（最简单）
    # result = quick_evaluate(mock_rag_query, test_questions)

    # 方式2：使用完整类（更灵活）
    rag_system = RAGSystemWrapper(query_func=mock_rag_query)
    evaluator = RAGEvaluator(rag_system)
    result = evaluator.run_evaluation(
        questions=test_questions,
        ground_truths=test_ground_truths,
    )

    print("\n评估完成！")
    print(f"诊断结果: {result.diagnosis}")
    print(f"Phase 1通过: {result.passed}")


if __name__ == "__main__":
    # 注意：实际运行需要：
    # 1. 设置 OPENAI_API_KEY 环境变量（或切换为Ollama/DeepSeek）
    # 2. 替换 mock_rag_query 为你的真实RAG系统
    main()