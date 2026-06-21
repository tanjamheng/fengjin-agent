# AI风堇 — 治愈晨昏

> 愿这一抹微光，治愈晨昏！

还原《崩坏：星穹铁道》中的风堇，一个有记忆、有知识、有安全边界的角色扮演 AI NPC。

## 项目特色

- **角色还原** — 详细的人物设定、性格、说话风格、知识边界，降低 OOC
- **长期记忆** — 跨会话记住用户偏好，自动提取、去重、合并、冲突消解
- **知识检索** — 混合 RAG（稠密 + 稀疏 + RRF 融合 + 交叉编码器重排序）
- **安全护栏** — 两级防护：P0 规则引擎（关键词/正则，毫秒级）+ P1 Llama Guard 3 1B（语义级）
- **双模型架构** — 主模型负责对话，辅助小模型负责记忆提取/合并，降低成本
- **桌面客户端** — Electron + TypeScript 原生前端，WebSocket 实时通信
- **双入口** — CLI 命令行 + WebSocket 桌面客户端，共用同一对话引擎

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt

# 前端（可选，仅桌面客户端需要）
cd frontend && npm install
```

### 2. 配置环境

```bash
cp .env.example .env
```

编辑 `.env`，填入你的 API Key：

```bash
# 主模型（OpenAI 兼容）
FENGJIN_API_KEY=your-api-key
FENGJIN_BASE_URL=https://open.bigmodel.cn/api/paas/v4
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
# 方式一：CLI 命令行（纯终端体验）
python main.py

# 方式二：WebSocket 服务 + 桌面客户端
python -m src.server.server          # 终端 1：启动后端 WS 服务
cd frontend && npm run dev           # 终端 2：启动 Electron 前端
```

### 4. 放置角色图（前端）

将风堇角色图放到 `frontend/src/renderer/assets/fengjin.jpg`，前端会自动加载。缺图时渐变背景兜底，不影响聊天功能。如需使用不同文件名，修改 `frontend/src/renderer/config.ts` 中的 `character.imagePath`。

## CLI 命令

| 命令 | 说明 |
|------|------|
| `/quit` | 退出并释放资源 |
| `/new` | 新建会话 |
| `/list` | 查看会话列表 |
| `/switch <编号>` | 切换到指定会话 |
| `/history` | 查看当前会话全部历史 |
| `/rename <标题>` | 重命名当前会话 |
| `/delete <编号>` | 删除指定会话 |
| `/clear` | 清空对话历史并新建会话 |
| `/ingest <文件路径>` | 导入单个文档到知识库 |
| `/ingest_dir <目录路径>` | 批量导入目录下的文档 |
| `/stats` | 查看知识库统计 |
| `/tools` | 查看已注册工具 |
| `/skills` | 查看已注册技能 |
| `/mcp` | 查看已注册 MCP 服务器 |

## 核心架构

### 对话管线

```
用户输入
  → 小伊卡安全检测（P0 规则引擎 → P1 Llama Guard）
      ├── BLOCK → 返回拦截提示
      └── PASS/COMFORT → 继续
  → 多轮上下文组装（记忆注入 + 滑动窗口裁剪）
  → LLM 流式生成（支持 Tool Calling 自主检索知识库，最多 5 轮）
  → 流式输出（CLI 打字效果 / WS 流式推送）
  → 异步记忆提取
  → 会话持久化
```

### 双入口架构

```
                    ┌─────────────────────┐
                    │  src/agent/streaming │  ← 共享对话引擎
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
    ┌─────────▼──────┐  ┌──────▼──────┐  ┌──────▼───────┐
    │  main.py (CLI)  │  │  ws/        │  │  frontend/    │
    │  终端交互        │  │  FastAPI    │  │  Electron     │
    │  rich 格式化     │  │  /ws 端点   │  │  TypeScript   │
    └─────────────────┘  └─────────────┘  └──────────────┘
```

### 双模型架构

| 模型 | 用途 | 协议 |
|------|------|------|
| 主模型（默认 GLM-5.1） | 对话推理、Tool Calling | OpenAI 兼容 |
| 辅助模型（默认 GLM-4.5-air） | 记忆提取/合并 | OpenAI 兼容 |

### 三种能力模型

| 能力 | 基类 | 触发者 | LLM 可见 | 用途 |
|------|------|--------|-----------|------|
| Skill | SkillBase | 系统代码 | 否 | 提示词注入（系统决定时机） |
| Tool | ToolBase | LLM | 是 | 函数调用（LLM 自主决定），返回 str |
| MCP | MCPServerBase | LLM | 是 | 标准化工具协议，注册时立即初始化 |

### 安全护栏

```
P0 规则引擎（毫秒级，零 LLM 调用）
  ├── 隐形字符检测（零宽空格、私用区字符）
  ├── 关键词匹配（9 类词表）
  └── 正则匹配
      ↓ 未命中
P1 Llama Guard 3 1B（语义级，懒加载）
  └── 13 类安全分类 → 统一映射到 11 类
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

### WebSocket 协议

前后端通过 `ws://127.0.0.1:8765/ws` 通信，17 种消息类型，支持流式推送、取消控制、心跳保持。完整协议定义见 `核心文档/核心4_WS通信协议.md`。

### 前端架构

Windows 桌面客户端，Electron 28 + TypeScript + 原生 HTML/CSS，无框架。

| 模块 | 职责 |
|------|------|
| CharacterDisplay | 角色展示：静态图片 + 渐变背景 + CSS 星光粒子 |
| WSClient + MessageParser | WebSocket 连接管理、心跳 30s、超时 60s、消息收发 |
| ChatUI + MessageRenderer + InputController | 对话区 DOM 渲染、流式打字效果、发送/停止互斥 |
| HistorySidebar | 会话列表、切换/删除/新建/清空 |
| state.ts | 中心状态管理（wsStatus / isReplying / sessions） |

安全策略：`contextIsolation: true, nodeIntegration: false, sandbox: true`，单实例锁。布局 38% / 42% / 20%，窗口 960×680 最小 800×520，粉蓝渐变自定义标题栏。前端配置集中在 `frontend/src/renderer/config.ts`。

## 技术栈

| 类别 | 技术 |
|------|------|
| LLM 调用 | `openai` SDK |
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
| Web 框架 | `FastAPI` + `uvicorn` |
| WebSocket | `ws`（starlette 内置） |
| 桌面客户端 | `electron` ≥ 28 + `electron-vite` ≥ 2 + TypeScript |
| 构建 | `electron-builder`（Portable 免安装） |

## 迭代计划

- [x] RAG 引擎 + Agent 框架
- [x] 记忆系统（提取 + 写入 + 检索 + 冲突消解）
- [x] 多轮对话上下文管理
- [x] 安全护栏（规则引擎 + Llama Guard）
- [x] WebSocket API + 会话管理
- [x] 前端桌面客户端 V1（Electron + TypeScript）
- [ ] OOC 率评估
- [ ] 性能评估
- [ ] 风堇 3D / Live2D 动画（V2）
- [ ] MCP 动作/表情调用

## 项目结构

```
AI风堇_治愈晨昏/
├── main.py                          # CLI 入口：启动序列 + 对话循环 + 命令路由
├── .env.example                     # 环境变量模板
├── requirements.txt                 # Python 依赖
├── CLAUDE.md                        # Claude Code 指令（红线 + 文件结构速查）
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
│       ├── *.txt                    # 关键词表
│       └── *.yaml                   # 正则规则
│
├── data/
│   ├── chroma/                      # RAG 向量库
│   ├── memory_chroma/               # 记忆向量库
│   └── sessions/                    # 会话 JSON 文件
│
├── models/                          # 本地模型（bge-m3 / bge-reranker-v2-m3 / Llama-Guard-3-1B）
├── logs/
│
├── src/                             # 后端核心实现
│   ├── agent/                       # Agent 核心（对话编排层）
│   │   ├── core.py                  # Agent 主类（CLI 对话循环 + Tool Calling）
│   │   ├── streaming.py             # 流式对话 service 层（CLI/WS 共用）
│   │   ├── stream_controller.py     # 流式取消机制
│   │   ├── context_manager.py       # 多轮上下文管理（记忆注入 + 滑动窗口裁剪）
│   │   ├── message_builder.py       # 共享消息组装（system_prompt + 回滚）
│   │   ├── skill_registry.py        # Skill 注册中心
│   │   ├── tool_registry.py         # Tool 注册中心
│   │   ├── mcp_manager.py           # MCP 管理器
│   │   └── prompt_template.py       # Prompt 模板引擎
│   │
│   ├── rag/                         # RAG 引擎（六层管线）
│   │   ├── rag_service.py           # RAG 服务门面
│   │   ├── a_loader.py              # 文档加载
│   │   ├── b_splitter.py            # 文本切分
│   │   ├── c_indexer.py             # 向量索引
│   │   ├── d_retriever.py           # 检索策略
│   │   ├── e_query_enhancer.py      # 查询增强
│   │   ├── f_reranker.py            # 重排序
│   │   ├── embedding_registry.py    # 嵌入模型进程级单例
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
│   │   ├── __init__.py              # SafetyManager（分层调度）
│   │   ├── rule_engine.py           # P0 规则引擎（关键词 + 正则）
│   │   ├── guard_model.py           # P1 Llama Guard 3 1B
│   │   └── loaders.py               # 词表/规则加载
│   │
│   ├── session/                     # 会话管理
│   │   ├── session.py               # Session / Message / MessageMeta
│   │   ├── store.py                 # 原子 JSON 读写（.tmp → os.replace）
│   │   ├── manager.py               # SessionManager — CRUD + flush
│   │   └── context_restorer.py      # 上下文恢复
│   │
│   ├── server/                      # WebSocket 服务入口
│   │   ├── app.py                   # FastAPI 工厂 + lifespan 单例加载
│   │   └── server.py                # uvicorn 启动入口
│   │
│   ├── ws/                          # WebSocket 传输适配层
│   │   ├── connection.py            # /ws 端点 + 消息路由 + 报文映射
│   │   └── schemas.py               # WS 协议 Pydantic 模型
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
│   ├── config.py                    # Pydantic 配置模型
│   └── utils/                       # 工具函数
│
├── frontend/                        # Electron 桌面客户端
│   ├── package.json
│   ├── electron.vite.config.ts
│   ├── electron-builder.yml
│   └── src/
│       ├── main.ts                  # Electron 主进程
│       ├── preload.ts               # IPC 桥接（窗口控制 API）
│       └── renderer/
│           ├── index.html           # 入口 HTML
│           ├── config.ts            # 前端配置中心
│           ├── state.ts             # 中心状态管理
│           ├── main.ts              # 渲染进程入口
│           ├── styles/main.css      # 全局样式 + CSS 变量
│           ├── assets/fengjin.jpg   # 角色展示图
│           ├── types/               # 协议类型定义
│           └── modules/
│               ├── character/       # 角色展示区
│               ├── chat/            # 对话区 UI
│               ├── sidebar/         # 历史侧边栏
│               └── ws/              # WebSocket 客户端
│
├── 核心文档/                        # 核心1(需求) 核心2(架构) 核心3(规范) 核心4(协议)
├── 前端开发核心文档/                 # 前端详细规格文档
├── 重要文档/                        # CR 流程 / 开发规范
├── 学习_多轮cr经验.md               # 后端 30 轮 CR 经验总结
└── 学习_前端8轮cr总结.md            # 前端 8 轮 CR 经验总结
```

## 许可证

MIT License
