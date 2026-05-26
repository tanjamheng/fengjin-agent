"""诊断RAG检索问题"""

import os
import sys
import math
from pathlib import Path

# 设置HF镜像
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Config, RAGSettings
from src.agent import Agent
from src.rag.c_indexer import Indexer
from src.rag.strategies.index.dense import DenseIndex

print("=" * 60)
print("RAG检索诊断")
print("=" * 60)

# 加载配置
config_path = Path(__file__).parent.parent / "config" / "config.yaml"
config = Config.load(str(config_path))

rag_config_path = Path(__file__).parent.parent / "config" / "rag.yaml"
rag_config = RAGSettings.load(str(rag_config_path))

# 直接测试索引器
print("\n[1] 初始化索引器...")
index_params = rag_config.rag.index.params
print(f"  embedding_model: {index_params.get('embedding_model')}")
print(f"  persist_directory: {index_params.get('persist_directory')}")
print(f"  collection_name: {index_params.get('collection_name')}")

index = DenseIndex(
    embedding_model=index_params.get('embedding_model'),
    persist_directory=index_params.get('persist_directory'),
    collection_name=index_params.get('collection_name'),
    store_type=index_params.get('store_type', 'chroma'),
    device=index_params.get('device', 'cpu')
)
index.initialize()

print(f"\n[2] 检查向量库状态...")
print(f"  文档数量: {index.count()}")

print(f"\n[3] 测试检索...")
query = "什么是RAG"
print(f"  查询: {query}")

raw_results = index.search(query, top_k=5)
print(f"  原始结果数: {len(raw_results)}")

print("\n[4] 分析检索结果...")
retriever_params = rag_config.rag.retriever.params
score_threshold = retriever_params.get('score_threshold', 0.7)
print(f"  score_threshold: {score_threshold}")

for i, item in enumerate(raw_results):
    distance = item.get('distance', 0)
    score = item.get('score', 0)

    # 模拟转换逻辑
    if score > 0:
        converted_score = score
    else:
        converted_score = math.exp(-distance / 10.0)

    passed = converted_score >= score_threshold

    print(f"\n  结果 {i+1}:")
    print(f"    distance: {distance:.4f}")
    print(f"    converted_score: {converted_score:.4f}")
    print(f"    threshold: {score_threshold}")
    print(f"    passed: {passed}")
    print(f"    content: {item['content'][:100]}...")

print("\n[5] 问题分析...")
if len(raw_results) == 0:
    print("  问题：向量库中没有文档！")
else:
    all_filtered = all(math.exp(-item.get('distance', 0) / 10.0) < score_threshold for item in raw_results)
    if all_filtered:
        print("  问题：所有结果被 score_threshold 过滤掉了！")
        print("  建议：降低 score_threshold 或调整转换公式")

        # 计算合适的阈值
        min_distance = min(item.get('distance', 0) for item in raw_results)
        suggested_threshold = math.exp(-min_distance / 10.0) * 0.8
        print(f"  最小distance: {min_distance:.4f}")
        print(f"  建议threshold: {suggested_threshold:.4f}")

print("\n" + "=" * 60)
print("诊断完成")
print("=" * 60)

index.cleanup()