"""知识库构建脚本

将数据侧_风堇资料下的文档导入向量知识库。
针对不同文档结构使用不同的分块策略：
  - markdown: 按 ##/### 标题切分，超长块自动递归二次切分（大多数文档）
  - recursive: 按分隔符递归切分（只有 # 级标题的大段落叙事文档）
用法：python scripts/build_knowledge.py
"""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.config import RAGSettings
from src.rag.a_loader import DocumentLoader
from src.rag.b_splitter import TextSplitter
from src.rag.c_indexer import Indexer

# ── 文档配置 ──────────────────────────────────────────
# 每个文件的分类和分块策略
# 策略选择依据：
#   markdown  — 有 ##/### 标题，按标题切分保留结构
#   recursive — 只有 # 级标题或纯文本，需要按字符数递归切分
DOCUMENT_CONFIG = {
    # 文件名: (分类, 分块策略, 策略参数)
    "自我定义.md": ("角色设定", "markdown", {
        "headers_to_split_on": ["##", "###"],
        "max_chunk_size": 1500,
        "chunk_overlap": 100,
    }),
    "角色故事.md": ("角色设定", "recursive", {
        # 只有 # 级标题，每个故事约 25-30 行，用递归分块保留叙事完整
        "chunk_size": 800,
        "chunk_overlap": 100,
    }),
    "角色语音.md": ("台词", "markdown", {
        "headers_to_split_on": ["##"],
        "max_chunk_size": 1500,
    }),
    "人物关系.md": ("人物关系", "markdown", {
        "headers_to_split_on": ["##"],
        "max_chunk_size": 1500,
    }),
    "同伴档案（待推敲）.md": ("人物关系", "markdown", {
        # ### 是每个人，按此切分最精确
        "headers_to_split_on": ["###"],
        "max_chunk_size": 1500,
    }),
    "剧情概述.md": ("剧情事件", "markdown", {
        "headers_to_split_on": ["##", "###"],
        "max_chunk_size": 1500,
    }),
    "剧情对话大全.md": ("剧情事件", "markdown", {
        # ## 是任务章节，### 是子片段；单章可达上千行，依赖二次递归切分
        "headers_to_split_on": ["##", "###"],
        "max_chunk_size": 1200,
        "chunk_overlap": 150,
    }),
    "风堇杂志提取.md": ("剧情事件", "markdown", {
        "headers_to_split_on": ["##", "###"],
        "max_chunk_size": 1500,
    }),
    "知识边界_翁法罗斯背景.md": ("世界观", "markdown", {
        # #### 是每个泰坦，按 ### 和 #### 切分
        "headers_to_split_on": ["###", "####"],
        "max_chunk_size": 1500,
    }),
}

# 未在 DOCUMENT_CONFIG 中配置的文件，使用此默认策略
DEFAULT_CONFIG = ("未分类", "markdown", {
    "headers_to_split_on": ["##", "###"],
    "max_chunk_size": 1500,
    "chunk_overlap": 100,
})


def main():
    data_dir = project_root / "数据侧_风堇资料"
    if not data_dir.exists():
        print(f"错误: 数据目录不存在 {data_dir}")
        sys.exit(1)

    print("初始化 RAG 组件（仅 loader + splitter + indexer）...")
    config = RAGSettings.load()
    rag_cfg = config.rag

    loader = DocumentLoader(
        supported_formats=rag_cfg.loader.supported_formats,
        max_file_size_mb=rag_cfg.loader.max_file_size_mb
    )
    indexer = Indexer(
        strategy_type=rag_cfg.index.type,
        strategy_params=rag_cfg.index.params
    )
    indexer.initialize()
    print("RAG 组件初始化完成\n")

    # 收集要导入的文件
    files_to_import = []
    for md_file in sorted(data_dir.glob("*.md")):
        category, strategy, strategy_params = DOCUMENT_CONFIG.get(
            md_file.name, DEFAULT_CONFIG
        )
        files_to_import.append((md_file, category, strategy, strategy_params))

    if not files_to_import:
        print("没有找到可导入的文档")
        sys.exit(0)

    print(f"准备导入 {len(files_to_import)} 个文档:\n")

    total_chunks = 0
    for file_path, category, strategy, strategy_params in files_to_import:
        # 为每个文档创建对应的分块器
        splitter = TextSplitter(
            strategy_type=strategy,
            strategy_params=strategy_params
        )

        # 加载文档
        document = loader.load(str(file_path), category=category)
        chunks = splitter.split_document(document)
        indexer.add(chunks)

        print(f"  [{category}] {file_path.name} ({strategy}) -> {len(chunks)} 个块")
        total_chunks += len(chunks)

    print(f"\n导入完成!")
    print(f"  文档数: {len(files_to_import)}")
    print(f"  总块数: {total_chunks}")
    print(f"  索引策略: {rag_cfg.index.type}")

    count = indexer.count()
    print(f"  知识库文档总数: {count}")

    indexer.cleanup()


if __name__ == "__main__":
    main()
