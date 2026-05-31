# Agent 使用指南

本文档面向 Agent 的使用者，介绍如何安装、配置和运行本项目。

---

## 1 环境要求

| 依赖 | 最低版本 |
|------|----------|
| Python | 3.10+ |
| pip | 最新稳定版 |
| Git | 任意版本 |

硬件方面，如果使用默认的 CPU 模式运行 Embedding 模型，不需要 GPU。如需 GPU 加速，需有 CUDA 环境。

---

## 2 安装步骤

### 2.1 克隆项目

```bash
git clone <项目地址>
cd 自研_agent+RAG
```

### 2.2 创建虚拟环境

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/macOS
source venv/bin/activate
```

### 2.3 安装依赖

```bash
pip install -r requirements.txt
```

如果在国内网络环境下，HuggingFace 模型下载可能较慢，可以设置镜像：

```bash
# 在 .env 中添加
HF_ENDPOINT=https://hf-mirror.com
```

---

## 3 配置

### 3.1 配置 API Key

复制环境变量模板并填入真实值：

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```env
# 必填：你的 API Key
FENGJIN_API_KEY=sk-your-real-key-here

# 必填：API 基础地址（根据你的服务商填写）
FENGJIN_BASE_URL=https://your-api-endpoint

# 可选：HuggingFace 镜像
HF_ENDPOINT=https://hf-mirror.com

# 可选：日志级别
LOG_LEVEL=INFO
```

> **重要**：`.env` 文件已在 `.gitignore` 中，不会被提交到版本库。**永远不要把 API Key 写入代码或配置文件中**。

### 3.2 配置 Agent 参数

编辑 `config/config.yaml`：

```yaml
agent:
  name: "SimpleAgent"       # Agent 名称
  model: "glm-5"            # 使用的模型
  max_tokens: 4096          # 最大生成 token 数
  temperature: 0.7          # 温度（越高越随机，越低越确定）

system_prompt: |
  你是一个有帮助的AI助手。请用中文回答用户的问题。
```

| 参数 | 说明 | 建议值 |
|------|------|--------|
| `model` | 调用的模型名称，需与你的 API 服务商支持的一致 | 取决于服务商 |
| `max_tokens` | 单次回复的最大 token 数 | 1024 ~ 8192 |
| `temperature` | 生成随机性 | 0.0（精确）~ 1.0（创意） |

### 3.3 配置 RAG 参数

编辑 `config/rag.yaml`，可以调整 RAG 管线各阶段的策略和参数。

#### 3.3.1 文档分块策略（splitter）

```yaml
splitter:
  type: "recursive"        # 可选：fixed / recursive / semantic / markdown
  params:
    chunk_size: 512        # 每块最大字符数
    chunk_overlap: 50      # 块之间的重叠字符数
```

| 策略 | 适用场景 | 特点 |
|------|----------|------|
| `fixed` | 简单文本 | 固定长度切分，不感知语义 |
| `recursive` | 通用（默认） | 按段落/句子递归切分，保持语义完整 |
| `semantic` | 高质量需求 | 基于语义相似度切分，需要 Embedding 模型 |
| `markdown` | Markdown 文档 | 按标题层级切分 |

#### 3.3.2 索引策略（index）

```yaml
index:
  type: "dense"            # 可选：dense / sparse / hybrid
  params:
    embedding_model: "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    persist_directory: "data/chroma"
    collection_name: "default"
    store_type: "chroma"
    device: "cpu"          # cpu / cuda
```

| 策略 | 适用场景 | 特点 |
|------|----------|------|
| `dense` | 语义检索（默认） | 向量相似度检索，适合自然语言问答 |
| `sparse` | 关键词精确匹配 | BM25 算法，适合专业术语检索 |
| `hybrid` | 综合场景 | dense + sparse 加权融合，效果最好但资源消耗最大 |

首次运行时，Embedding 模型会自动从 HuggingFace 下载（约 400MB），下载后会缓存到本地。

#### 3.3.3 检索策略（retriever）

```yaml
retriever:
  type: "top_k"            # 可选：top_k / hybrid / parent_doc / hyde
  params:
    top_k: 5               # 返回最相关的 K 个结果
    score_threshold: 0.25  # 相似度阈值，低于此值的结果会被过滤
```

| 策略 | 适用场景 | 特点 |
|------|----------|------|
| `top_k` | 通用（默认） | 返回最相似的 K 个文档块 |
| `hybrid` | dense + sparse 结合 | 需要同时启用 hybrid index |
| `parent_doc` | 需要更大上下文 | 先检索小块，再返回其所属的大块 |
| `hyde` | 查询模糊时 | 用 LLM 生成假设性回答来辅助检索 |

#### 3.3.4 重排序策略（reranker）

```yaml
reranker:
  type: "none"             # 可选：none / cross_encoder / llm
  params:
    top_n: 3               # 重排后保留的结果数
```

| 策略 | 适用场景 | 特点 |
|------|----------|------|
| `none` | 不需要重排（默认） | 直接使用检索结果 |
| `cross_encoder` | 高精度需求 | 使用交叉编码器模型重排序，精度高但慢 |
| `llm` | 复杂场景 | 用 LLM 对结果逐一评分，最灵活但成本高 |

#### 3.3.5 查询增强策略（query_enhancer）

```yaml
query_enhancer:
  type: "none"             # 可选：none / rewrite / decompose / expand
  params: {}
```

| 策略 | 适用场景 | 特点 |
|------|----------|------|
| `none` | 不需要增强（默认） | 直接使用原始查询 |
| `rewrite` | 查询表述不清 | 用 LLM 重写查询 |
| `decompose` | 复杂多意图查询 | 将一个问题拆解为多个子查询 |
| `expand` | 查询覆盖面窄 | 生成多个相关查询扩大检索范围 |

---

## 4 运行

### 4.1 启动 Agent

```bash
python main.py
```

启动后会看到欢迎界面：

```
╭─────────── Agent 启动 ───────────╮
│ SimpleAgent                       │
│ 模型: glm-5                       │
│ 已装配 Skills: rag                │
│ 输入 /quit 退出                   │
│ 输入 /clear 清空对话历史           │
│ 输入 /rag 开启 RAG 模式            │
│ 输入 /ingest <文件路径> 导入文档    │
│ 输入 /stats 查看知识库状态          │
│ 输入 /skills 查看已装配技能         │
╰──────────────────────────────────╯
```

### 4.2 基础对话

直接输入文字即可与 Agent 对话：

```
你: 你好，请介绍一下自己
Agent:
你好！我是 SimpleAgent，一个有帮助的AI助手...
对话轮数: 1
```

### 4.3 使用 RAG 模式

RAG（检索增强生成）模式可以让 Agent 基于你的私有文档回答问题。

#### 第一步：导入文档

```
你: /ingest docs/my_document.pdf
```

支持批量导入整个目录：需要在代码中调用 `rag_skill_instance.ingest_directory(dir_path)`（参见下方进阶用法）。

支持的文件格式：`pdf`、`md`、`txt`、`docx`。

导入成功后会显示切分出的文本块数量。

#### 第二步：开启 RAG 模式

```
你: /rag
RAG 模式已开启
```

再次输入 `/rag` 可以关闭。

#### 第三步：提问

```
你: /rag
你: 这个文档主要讲了什么？
Agent: (Skills: rag)
根据提供的资料，这个文档主要讲述了...
```

当 RAG 模式开启时，Agent 会在回答前先从知识库检索相关内容，然后基于检索到的内容生成回答。

### 4.4 查看知识库状态

```
你: /stats
```

会显示一个表格，包含：是否已初始化、文档数量、各阶段使用的策略类型。

### 4.5 查看已装配的 Skills

```
你: /skills
```

会列出所有已注册的 Skill 的名称、描述和版本。

### 4.6 退出

```
你: /quit
再见！
```

或按 `Ctrl+C` 退出。

---

## 5 命令速查表

| 命令 | 说明 |
|------|------|
| `/rag` | 开启/关闭 RAG 模式（开关切换） |
| `/ingest <文件路径>` | 导入单个文档到知识库 |
| `/stats` | 查看知识库统计信息 |
| `/skills` | 查看已装配的所有 Skill |
| `/clear` | 清空对话历史 |
| `/quit` | 退出 Agent |

---

## 6 目录结构说明

```
自研_agent+RAG/
├── main.py              # CLI 入口，启动和命令循环
├── requirements.txt     # Python 依赖列表
├── .env                 # API Key 等敏感信息（不提交到 Git）
├── .env.example         # 环境变量模板
├── config/
│   ├── config.yaml      # Agent 配置（模型名称、系统提示词等）
│   ├── config.example.yaml  # 配置模板
│   └── rag.yaml         # RAG 管线各阶段策略配置
├── src/
│   ├── agent/           # Agent 核心（Skill 注册、对话管理）
│   ├── skills/          # Skill 实现（目前有 RAG Skill）
│   ├── rag/             # RAG 六阶段管线
│   │   ├── strategies/  # 每阶段的可替换策略
│   │   └── ...
│   ├── config.py        # 配置 Pydantic 模型
│   └── utils/           # 工具函数（日志、路径）
├── data/chroma/         # ChromaDB 向量存储（自动创建）
├── logs/                # 日志文件（自动创建）
├── docs/                # 文档
└── tests/               # 测试
```

---

## 7 常见问题

### Q: 启动报错 `请在 .env 文件中设置 FENGJIN_API_KEY`

确认 `.env` 文件存在且已填入有效的 API Key。检查是否在项目根目录下。

### Q: 首次运行很慢，卡在 Embedding 模型下载

首次使用 dense 索引时，需要从 HuggingFace 下载 Embedding 模型（约 400MB）。国内用户建议设置 `HF_ENDPOINT=https://hf-mirror.com`。

### Q: RAG 检索结果不准确

可以尝试以下调整：
1. 降低 `score_threshold`（在 `rag.yaml` 的 `retriever.params` 中）
2. 增大 `top_k` 值
3. 更换分块策略（如从 `fixed` 改为 `recursive`）
4. 开启重排序（`reranker.type` 改为 `cross_encoder` 或 `llm`）
5. 开启查询增强（`query_enhancer.type` 改为 `rewrite` 或 `decompose`）

### Q: 导入 PDF 报错

确保安装了 PDF 解析库：`pip install pypdf` 或 `pip install PyMuPDF`。

### Q: 如何更换模型

编辑 `config/config.yaml` 中的 `model` 字段，并在 `.env` 中更新 `FENGJIN_BASE_URL` 为对应服务商的地址。

---

## 8 日志说明

日志文件保存在 `logs/` 目录下，按日志级别分文件：

- `logs/agent_info.log` — INFO 及以上级别的日志
- `logs/agent_json.log` — JSON 格式日志（需在 `main.py` 中启用 `json_format=True`）

每条日志都带有 `trace_id`，用于追踪一次完整的请求链路。日志包含敏感信息自动脱敏（如 API Key 会被替换为 `***REDACTED***`）。
