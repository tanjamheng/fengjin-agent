"""测试RAG完整流程"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Config, RAGSettings
from src.agent import Agent
from src.rag.rag_service import RAGService
from src.mcp_servers.rag_server import RAGMCPServer

print("=" * 60)
print("测试 RAG 完整流程")
print("=" * 60)

# 加载配置
config_path = Path(__file__).parent.parent / "config" / "config.yaml"
config = Config.load(str(config_path))

rag_config_path = Path(__file__).parent.parent / "config" / "rag.yaml"
rag_config = RAGSettings.load(str(rag_config_path))

# 创建Agent和RAG服务
agent = Agent(config)
rag_service = RAGService(rag_config, llm_client=agent.client)
agent.register_mcp(RAGMCPServer(rag_service))

# Step 1: 导入文档
print("\n[Step 1] 导入文档...")
test_doc = Path(__file__).parent.parent / "data" / "rag_intro.md"
if test_doc.exists():
    try:
        result = rag_service.ingest_document(str(test_doc))
        print(f"  成功! 生成 {result['chunk_count']} 个文本块")
    except Exception as e:
        print(f"  失败: {e}")
else:
    print(f"  文档不存在: {test_doc}")

# Step 2: 查看知识库状态
print("\n[Step 2] 知识库状态...")
stats = rag_service.get_stats()
for key, value in stats.items():
    print(f"  {key}: {value}")

# Step 3: 测试RAG对话
print("\n[Step 3] 测试RAG对话...")
questions = [
    "什么是RAG？",
    "RAG有哪些核心组件？",
    "如何评估RAG系统？"
]

for q in questions:
    print(f"\n问题: {q}")
    response = agent.chat(q)
    print(f"回答: {response[:200]}...")

print("\n" + "=" * 60)
print("RAG测试完成!")
print("=" * 60)

agent.cleanup()
