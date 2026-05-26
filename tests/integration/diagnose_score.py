"""诊断检索score阈值问题"""

import os
import sys
import math
from pathlib import Path

os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.rag.c_indexer import Indexer
from src.rag.strategies.index.dense import DenseIndex
from src.rag.strategies.splitter import get_splitter
from src.rag.a_loader import DocumentLoader
from src.config import RAGSettings

print("=" * 60)
print("诊断 score_threshold 问题")
print("=" * 60)

# 加载配置
rag_config_path = Path(__file__).parent.parent / "config" / "rag.yaml"
rag_config = RAGSettings.load(str(rag_config_path))

# 初始化索引器
index_params = rag_config.rag.index.params
index = DenseIndex(
    embedding_model=index_params.get('embedding_model'),
    persist_directory=index_params.get('persist_directory'),
    collection_name=index_params.get('collection_name'),
    store_type=index_params.get('store_type', 'chroma'),
    device=index_params.get('device', 'cpu')
)
index.initialize()

# 检查文档数
print(f"\n[1] 向量库文档数: {index.count()}")

# 如果没有文档，先导入
if index.count() == 0:
    print("  没有文档，开始导入...")
    loader = DocumentLoader()
    doc = loader.load(str(Path(__file__).parent.parent / "data" / "rag_intro.md"))

    splitter_params = rag_config.rag.splitter.params
    splitter = get_splitter(rag_config.rag.splitter.type, splitter_params)
    chunks = splitter.split(doc)

    index.add(chunks)
    print(f"  导入完成，文档数: {index.count()}")

# 直接调用ChromaDB查询，绕过score过滤
print("\n[2] 直接查询ChromaDB（无过滤）...")
query = "什么是RAG"
query_embedding = index._embed([query])[0]
raw_results = index._collection.query(
    query_embeddings=[query_embedding],
    n_results=5
)

print(f"  原始结果数: {len(raw_results['documents'][0])}")

for i, (doc, dist) in enumerate(zip(raw_results['documents'][0], raw_results['distances'][0])):
    # 计算转换后的score
    converted_score = math.exp(-dist / 10.0)
    print(f"\n  结果 {i+1}:")
    print(f"    distance (L2): {dist:.4f}")
    print(f"    converted_score: {converted_score:.4f}")
    print(f"    threshold (config): {rag_config.rag.retriever.params.get('score_threshold', 0.7)}")
    print(f"    是否通过阈值: {converted_score >= rag_config.rag.retriever.params.get('score_threshold', 0.35)}")
    print(f"    内容片段: {doc[:100]}...")

# 分析问题
print("\n[3] 问题分析...")
threshold = rag_config.rag.retriever.params.get('score_threshold', 0.35)
min_dist = min(raw_results['distances'][0])
max_score = math.exp(-min_dist / 10.0)

print(f"  最小distance: {min_dist:.4f}")
print(f"  最大转换score: {max_score:.4f}")
print(f"  当前阈值: {threshold}")

if max_score < threshold:
    print(f"\n  问题确认：所有结果的score都低于阈值！")
    print(f"  建议：将 threshold 改为 {max_score * 0.5:.2f} 或更低")
else:
    print(f"\n  阈值设置合理，应该有结果通过")

# 测试用更低阈值检索
print("\n[4] 用调整后阈值测试...")
# 临时修改阈值测试
from src.rag.d_retriever import Retriever
retriever_params = rag_config.rag.retriever.params.copy()
retriever_params['score_threshold'] = 0.1  # 临时降低

retriever = Retriever(
    strategy_type='top_k',
    strategy_params=retriever_params
)
retriever.initialize(index)

results = retriever.retrieve(query)
print(f"  threshold=0.1 时，检索结果数: {len(results)}")

for i, r in enumerate(results[:3]):
    print(f"  结果{i+1} score={r.score:.3f}: {r.content[:80]}...")

print("\n" + "=" * 60)
print("诊断完成")
print("=" * 60)