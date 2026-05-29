# AI风堇 — 治愈晨昏

> 愿这一抹微光，治愈晨昏！

还原《崩坏：星穹铁道》中的风堇，一个有记忆、有知识、有安全边界的角色扮演 AI NPC。

## 项目特色

- **角色还原** — 详细的人物设定、性格、说话风格、知识边界，降低 OOC
- **长期记忆** — 跨会话记住用户偏好，自动提取、去重、合并、冲突消解
- **知识检索** — 混合 RAG（稠密 + 稀疏 + RRF 融合 + 交叉编码器重排序）
- **安全护栏** — 两级防护：P0 规则引擎（关键词/正则，毫秒级）+ P1 Llama Guard 3 1B（语义级）
- **双模型架构** — 主模型负责对话，辅助小模型负责记忆提取/合并，降低成本

## 项目结构

```
AI风堇_治愈晨昏/
├── main.py                          # CLI 入口
├── .env.example                     # 环境变量模板
├── requirements.txt                 # 依赖管理
├── CLAUDE.md                        # Claude Code 指令
│
├── config/                          # 配置层
│   ├── config.yaml                  # Agent 主配置
│   ├── config.example.yaml          # 配置示例
│   ├── rag.yaml                     # RAG 管线配置
│   ├── memory.yaml                  # 记忆系统配置
│   ├── context.yaml                 # 上下文管理配置
│   ├── safety.yaml                  # 安全护栏配置
│   ├── system_prompt.md             # 风堇角色设定
│   ├── prompts/                     # Prompt 模板
│   │   ├── core_memory.md           # 核心记忆视图
│   │   ├── memory_extraction.md     # 记忆提取提示词
│   │   └── memory_merge.md          # 记忆合并提示词
│   └── safety_words/                # 安全词表
│       ├── *.txt                    # 9 类关键词表
│       └── regex_patterns.yaml      # 正则规则
│
├── data/
│   └── memory_chroma/               # 记忆向量库（ChromaDB）
│
├── src/                             # 核心实现
│   ├── agent/                       # Agent 核心
│   │   ├── core.py                  # Agent 主类（11 步对话管线）
│   │   ├── context_manager.py       # 多轮上下文管理（滑动窗口 + 记忆注入）
│   │   ├── skill_registry.py        # Skill 注册中心
│   │   ├── tool_registry.py         # Tool 注册中心
│   │   ├── mcp_manager.py           # MCP 管理器
│   │   └── prompt_template.py       # Prompt 模板管理
│   │
│   ├── rag/                         # RAG 引擎（六层管线）
│   │   ├── a_loader.py              # 文档加载
│   │   ├── b_splitter.py            # 文本切分
│   │   ├── c_indexer.py             # 向量索引
│   │   ├── d_retriever.py           # 检索策略
│   │   ├── e_query_enhancer.py      # 查询增强
│   │   ├── f_reranker.py            # 重排序
│   │   ├── rag_service.py           # RAG 服务层
│   │   └── strategies/              # 策略仓库
│   │       ├── splitter/            # fixed, recursive, semantic, markdown
│   │       ├── index/               # dense, sparse, hybrid
│   │       ├── retriever/           # top_k, hybrid, hyde, parent_doc
│   │       ├── query/               # rewrite, expand, decompose
│   │       └── reranker/            # none, llm, cross_encoder
│   │
│   ├── memory/                      # 记忆系统
│   │   ├── manager.py               # 记忆管理器（门面）
│   │   ├── extractor.py             # 记忆提取（LLM + 规则过滤）
│   │   ├── writer.py                # 异步写入（队列 + 冲突消解）
│   │   ├── retriever.py             # 双层检索（核心记忆文件 + 向量搜索）
│   │   ├── storage.py               # ChromaDB 存储层
│   │   └── config.py                # 记忆配置模型
│   │
│   ├── safety/                      # 安全护栏
│   │   ├── safety_manager.py        # 安全管理器（分层调度）
│   │   ├── rule_engine.py           # P0 规则引擎（关键词 + 正则）
│   │   ├── guard_model.py           # P1 Llama Guard 3 1B
│   │   └── loaders.py               # 词表/规则加载
│   │
│   ├── capabilities/                # 能力抽象层
│   │   ├── skill.py                 # Skill 基类
│   │   ├── tool.py                  # Tool 基类
│   │   └── mcp_server.py            # MCP 服务器基类
│   │
│   ├── mcp_servers/                 # MCP 服务器实例
│   │   └── rag_server.py            # RAG MCP 服务器
│   │
│   ├── skills/                      # 技能插件
│   ├── config.py                    # 配置管理
│   └── utils/                       # 工具函数
│
├── tests/                           # 测试
└── 学习文档/                         # 学习笔记
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境

```bash
cp .env.example .env
```

编辑 `.env`，填入你的 API Key：

```bash
# 主模型（Anthropic 兼容）
FENGJIN_API_KEY=your-api-key
FENGJIN_BASE_URL=https://open.bigmodel.cn/api/anthropic
FENGJIN_MODEL=glm-5.1

# 记忆辅助模型（OpenAI 兼容）
MEMO_API_KEY=your-api-key
MEMO_BASE_URL=https://open.bigmodel.cn/api/paas/v4
MEMO_MODEL=glm-4.5-air

# 国内 HuggingFace 镜像
HF_ENDPOINT=https://hf-mirror.com
```

### 3. 启动

```bash
python main.py
```

## CLI 命令

| 命令 | 说明 |
|------|------|
| `/quit` | 退出并释放资源 |
| `/clear` | 清空对话历史 |
| `/ingest <文件路径>` | 导入单个文档到知识库 |
| `/ingest_dir <目录路径>` | 批量导入目录下的文档 |
| `/stats` | 查看知识库统计 |
| `/tools` | 查看已注册工具 |
| `/skills` | 查看已注册技能 |
| `/mcp` | 查看已注册 MCP 服务器 |

## 核心架构

### 对话管线（11 步）

```
用户输入
  → 小伊卡安全检测（P0 规则引擎 → P1 Llama Guard）
  → Skill 注入
  → 上下文管理（记忆检索 + 注入）
  → 构建 API 参数（system prompt + 安全上下文）
  → 流式调用 LLM
  → Tool Calling 循环（最多 5 轮）
  → 回复追加到历史
  → 滑动窗口裁剪
  → 异步记忆提取
  → 流式输出到前端
```

### 双模型架构

| 模型 | 用途 | 协议 |
|------|------|------|
| 主模型（默认 GLM-5.1） | 对话推理 | Anthropic 兼容 |
| 辅助模型（默认 GLM-4.5-air） | 记忆提取/合并 | OpenAI 兼容 |

### 安全护栏

```
P0 规则引擎（毫秒级，零 LLM 调用）
  ├── 隐形字符检测（零宽空格、私用区字符）
  ├── 关键词匹配（9 类词表）
  └── 正则匹配
      ↓ 未命中
P1 Llama Guard 3 1B（语义级，懒加载）
  └── 12 类安全分类 → 统一映射到 9 类
```

三种动作：`BLOCK`（拦截）、`COMFORT`（注入安慰提示词，不中断对话）、`PASS`（放行）

### 记忆系统

```
对话结束
  → LLM 提取事实（json_object 格式，最多 2 次重试）
  → 规则过滤（PII 黑名单 + 向量去重）
  → 队列写入（后台线程串行）
    ├── 相似度 < 0.1 → 去重跳过
    ├── 冲突检测 → LLM 合并
    └── 新事实 → 写入 ChromaDB
  → 核心记忆保护（低重要性不可覆盖高重要性）
  → 自动刷新 core_memory.md

检索：
  → 全文读取 core_memory.md（~1ms）
  → ChromaDB 语义搜索 top-K（~30ms）
  → 格式化注入对话上下文
```

### RAG 管线

```
文档 → 加载（PDF/DOCX/MD/TXT）
    → 切分（fixed / recursive / semantic / markdown）
    → 索引（稠密 BGE-M3 + 稀疏 BM25 + 混合 RRF）
    → 检索（top_k / hybrid / HyDE / parent_doc）
    → 查询增强（rewrite / expand / decompose）
    → 重排序（none / LLM / BGE-reranker-v2-m3 交叉编码器）
    → 返回结果
```

### 上下文管理

- **滑动窗口**：双限制裁剪 — 20 轮对话 / ~4000 token
- **记忆注入**：检索相关记忆，通过模板注入用户消息

## 技术栈

| 类别 | 技术 |
|------|------|
| LLM 调用 | `anthropic` SDK、`openai` SDK |
| 向量数据库 | `chromadb` |
| 嵌入模型 | BGE-M3（sentence-transformers） |
| 重排序 | BGE-reranker-v2-m3 |
| 稀疏检索 | `rank_bm25` |
| 文本切分 | `langchain-text-splitters` |
| 文档解析 | `pypdf`、`python-docx` |
| 安全模型 | Llama Guard 3 1B（transformers） |
| 配置 | `pydantic`、`pyyaml` |
| 日志 | `loguru` |
| CLI | `rich` |

## 迭代计划

- [x] RAG 引擎 + Agent 框架
- [x] 记忆系统（提取 + 写入 + 检索 + 冲突消解）
- [x] 多轮对话上下文管理
- [x] 安全护栏（规则引擎 + Llama Guard）
- [ ] OOC 率评估
- [ ] 性能评估
- [ ] 前端对话界面
- [ ] 风堇 3D / Live2D 动画
- [ ] MCP 动作/表情调用

## 许可证

MIT License
