# AI风堇立项书

愿这一抹微光，拨开云雾，重见晴空！

## 项目愿景

制作游戏AI NPC，还原《崩坏：星穹铁道》中的风堇，与我对话，治愈我。
我会永远维护这个项目，永远优化下去，让ai越来越还原风堇。

## 迭代目标

1. AI能够越来越还原风堇，甚至还原三观性格。
2. 降低出错率，穿帮率。
3. 前端风堇动画尽可能唯美。

# 当前任务

# 核心文档
核心文档\核心1_需求梳理.md
核心文档\核心2_技术架构.md
核心文档\核心3_开发规范.md
核心文档\核心4_WS通信协议.md
这四个文档将是开发的最高宗旨，不容违反。一切开发将以其为锚点。
其中的核心内容已写入CLAUDE.md中，如有必要，可以进行复习这些文档中的内容。
每当开发新功能时，也会将新功能相关的需求，技术架构等写入这些核心文档中。

## CLAUDE.md 维护规则

本文档是核心1/2/3/4 的**衍生速查**——红线、文件结构、技术约束均从核心文档提取。**核心文档是权威源，本文档是工作副本。**

以下变化必须同步更新本文档：
- 核心3 红线速查新增/修改/删除条目 → 同步本文档「红线速查」
- 核心4 WS 协议消息类型新增/修改/删除 → 同步本文档「WS 协议要点」表格
- 核心2 文件结构树新增/删除/重命名文件 → 同步本文档「文件结构速查」
- 清理链/初始化顺序变更 → 同步本文档「清理链」
- 核心文档新增关键约束或技术决策 → 同步本文档「技术约束」

**每次开发对话结束时**，如果本次修改了核心文档 → AI 必须主动检查本文档是否需要同步。最危险的情况：核心3 加了新红线但本文档没加 → AI 在后续工作中不会遵守那条规则——因为 AI 只看 CLAUDE.md。

## 陷阱速查（本项目踩过的坑）

| # | 陷阱 | 说明 |
|---|------|------|
| 1 | **PowerShell 中不要用 `git commit -m @'...'@`** | here-string 的 `@'` 会被当作文本的一部分混入提交消息，导致消息以 `@ ` 开头。正确做法：先 `$msg = @'...'@` 赋值变量，再 `git commit -m $msg` |
| 2 | **禁止提交任何中文文档** | `核心文档/`、`重要文档/`、`前端开发核心文档/`、`*.md`（中文设计/规范/流程文档）已被 `.gitignore` 忽略。git add 会报 `ignored by .gitignore`。**永远不要用 `-f` 强制提交中文文档**——它们是本地工作副本，不入仓库。只提交 `src/`、`frontend/src/`、`config/`、`main.py`、`requirements.txt`、`CLAUDE.md`、`start.bat` 等代码文件 |
| 3 | **`.bat` 中 PowerShell inline 命令的 `%` 必须写成 `%%`** | CMD 会把 `%` 当变量前缀吃掉——`$i % 15` 会变成 `$i  15`（`%` 被吞 → 后面的数字变成裸 token → PowerShell 语法错误）。正确写法：`$i %% 15`（CMD 将 `%%` 转义为 `%` 传给 PowerShell）。任何传给 PowerShell 的 `%` 都要双写 |
| 4 | **禁止未经允许执行 `git commit`** | 所有 git 提交必须等用户明确说"提交"/"commit"之后才能执行。用户没开口 = 不准 commit。**例外：Code Review 循环中每轮修复后允许自动提交**（CR 流程本身要求每轮 commit） |

# 功能速查

## 核心链路

用户输入 → Agent.chat() 统一管线（CLI/WS 共用）→ 小伊卡安全检测（P0规则 + P1 Llama Guard）→ 多轮上下文组装（情绪注入 + 记忆注入 + 滑动窗口裁剪）→ LLM 流式生成（stream_llm，支持 Tool Calling 自主检索知识库，最多 5 轮）→ 流式输出 → LLM 回复末尾提取情绪标记 → EMA 平滑更新情绪状态 → 异步记忆提取（Writer WAL 崩溃恢复）→ 会话持久化

## 后端已有能力

| 模块 | 做了什么 |
|------|---------|
| 对话引擎 | 流式对话 + Tool Calling 循环 + 停止/超时处理 |
| 风堇角色系统 | 外部 system_prompt.md 定义人设，调角色不改代码 |
| 情绪状态机 | PAD 三维情绪 + EMA 平滑 + 非对称指数衰减，LLM 输出隐藏标记，数字注入 user message |
| RAG 知识库 | 6 步管道检索风堇相关知识，LLM 自主决定调用时机 |
| 记忆系统 | 跨会话记住用户信息，双存储（core_memory.md + ChromaDB），异步提取 |
| 安全护栏 | 两级检测（规则引擎 + Llama Guard 3 1B），11 类拦截，Comfort 安抚模式 |
| 会话管理 | JSON 原子写入，14 个 CLI 命令（含会话、知识库管理、调试） |
| WebSocket API | FastAPI + /ws 端点，流式推送 + 取消控制（前端联调用） |

## 前端（V1 已实现）

> **前端开发时，本节是唯一需要看的核心文档内容。** 详细规格查 `前端开发核心文档/`（1=功能边界 2=UI像素 3=架构类接口），WS 协议查 `核心文档/核心4_WS通信协议.md`。

### 交付形态

Windows 桌面客户端，Electron ≥ 28.x + TypeScript + 原生 HTML/CSS，WebSocket 通信。构建工具 electron-vite，打包 electron-builder（Portable 免安装）。

### 核心约束（违反即错）

- **不引入 React / Vue / Svelte** — 单页应用，原生 DOM 足够
- **不引入 CSS 框架**（Tailwind 等）— 手写 CSS，CSS 变量统一管理配色
- **不引入状态管理库** — 全局状态极少，用中心状态对象 + 回调
- **TypeScript 禁止滥用 `any`** — 所有后端通信数据必须有 Interface 定义
- **前后端通信只走 WebSocket** — `ws://127.0.0.1:8765/ws`，不引入 REST API
- **布局比例固定** — 左 38%（角色展示）+ 中 45%（对话区）+ 右 17%（历史侧边栏），V2 不变
- **窗口** — 默认 960×680，最小 800×520，自定义粉蓝渐变标题栏（`#FFBACC → #9AC2FF`）
- **单实例锁** — `app.requestSingleInstanceLock()`，防止多窗口 WebSocket 冲突

### 安全策略（Electron 固定值，不可改）

`contextIsolation: true, nodeIntegration: false, sandbox: true`

### 核心交互主链路

```
用户输入 → 前端锁定输入（禁止二次发送）
  → WS 发送 user_msg → 后端处理（安全→上下文→LLM）
  → 被拦截？→ 显示小伊卡提示，解锁
  → 正常？→ 流式打字效果逐字呈现
  → 用户点停止？→ 发送 cancel 信号，保留已显示文字，解锁
  → 完成？→ 固化完整文本，解锁
  → 超时 >60s？→ 显示"回复超时"，解锁
```

### 五大模块

| 模块 | 文件 | 职责 |
|------|------|------|
| 角色展示 | `modules/character/CharacterDisplay.ts` | V1 静态 JPG + 渐变背景 + CSS 星光粒子 |
| WS 通信 | `modules/ws/WSClient.ts` + `MessageParser.ts` | 连接管理 + 心跳 30s + 10s 超时 + 消息收发 |
| 聊天 UI | `modules/chat/ChatUI.ts` + `MessageRenderer.ts` + `InputController.ts` | DOM 渲染 + 流式拼接 + 滚动 + 发送/停止 |
| 历史侧边栏 | `modules/sidebar/HistorySidebar.ts` | 会话列表/切换/删除，通过 WSClient 调后端 SessionManager |
| 状态管理 | `state.ts` | `wsStatus / isReplying / isModelLoaded / isScrolledToBottom / currentSessionId / sessions` |

### 状态联动

- `isReplying === true` → 发送按钮变为红色"停止"，侧边栏其他会话灰不可点
- `wsStatus !== "connected"` → 发送 disabled，状态栏离线
- `isScrolledToBottom === false` → 显示"↓ 有新消息"浮动提示

### IPC 通信

Preload 只暴露窗口控制 API（最小化/最大化/关闭/置顶）。渲染进程通过原生浏览器 API（WebSocket、DOM）工作。

### 配色（CSS 变量速查）

`--color-bg-chat: #F8F8F8` / `--color-bubble-ai: #FFE6F2` / `--color-bubble-user: #F9F2EB` / `--color-input-bg: #D0E4FE` / `--color-input-border: #BACCFE` / `--color-titlebar-start: #FFBACC` / `--color-titlebar-end: #9AC2FF` / `--color-star: #F5C842` / `--color-blocked: #E8A050` / `--color-status-online: #50C878` / `--color-status-offline: #E05555`

### 字体

全局字体栈：`"PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif`，对话 14px，辅助 12px，行高 1.6。

### WS 协议要点

| 前端→后端 | 后端→前端 |
|-----------|----------|
| `user_msg` (session_id, content) | `connected` (session_id) |
| `ping` (每 30s) | `pong` |
| `cancel` | `thinking` |
| `list_sessions` | `stream` (text 分片) |
| `load_session` (session_id) | `end` (full_text, action) |
| `delete_session` (session_id) | `blocked` (message, category) |
| | `session_list` / `session_loaded` / `session_deleted` |
| | `quick_replies` (可选，最多3条) / `error` |

> 完整字段定义 + 时序图 → `核心文档/核心4_WS通信协议.md`
> TypeScript 类型定义 → `frontend/src/types/protocol.ts`

### 文档导航

| 需要什么 | 去这里 |
|---------|--------|
| 功能边界 + 边界情况 | `前端开发核心文档/1.功能需求说明书.md` |
| UI 像素级规范 | `前端开发核心文档/2.UI与页面规范文档.md` |
| 架构 + 类接口 + 打包 | `前端开发核心文档/3.系统架构与技术选型文档.md` |
| WS 协议完整字段 | `核心文档/核心4_WS通信协议.md` |
| 前端编码约束 + 红线 | `核心文档/核心3_开发规范.md` 第三/四章 |

# 术语表

| 术语 | 含义 |
|------|------|
| 风堇 | 《崩坏：星穹铁道》角色，AI NPC 扮演对象 |
| 小伊卡 | 安全护栏的角色内人格——以角色口吻拦截不当输入 |
| Comfort 模式 | 自杀/自伤内容不直接拦截，改为注入安抚指令到 system_prompt |
| 翁法罗斯 | 游戏世界观名称，知识库内容限定范围 |
| Skill | 系统注入能力——LLM 不可见，由系统代码决定时机 |
| Tool | LLM 可调用的函数——通过 function calling 暴露，返回 str |
| MCP | 标准化工具协议——MCPServerBase 子类，注册时立即初始化 |
| source | 日志模块标识——调用 `get_logger("source")` 时必传的可读字符串（如 `"ws"`, `"core"`）。禁止传 uuid 或留空 |  
| trace_id | 每次对话生成的唯一追踪 ID（8位hex）——贯穿日志、会话、记忆全链路。非请求事件自动填充 `--------` |
| RAG | 检索增强生成——6 步管道（加载→切分→索引→查询增强→检索→重排序） |
| bge-m3 | 嵌入模型 ~1.1GB——将文本转为向量，供 DenseIndex 和 MemoryStorage 使用 |
| bge-reranker-v2-m3 | Cross-Encoder 重排序模型 ~1.1GB——对检索结果精排 |
| Llama Guard 3 1B | Meta 安全语义检测模型（FP16 ~2GB 内存）——P1 防线，13 类语义分类。默认关闭，由 `FENGJIN_GUARD_MODEL_ENABLED` 控制 |
| ChromaDB | 向量数据库——RAG 和 Memory 各一个 PersistentClient |
| Tool Calling | LLM 自主决定调用工具的能力——本项目上限 5 轮 |
| StreamController | 流式取消机制——协作式 cancel flag + task.cancel() 兜底 |
| StreamInterrupted | 流式中断异常——客户端断连时 on_token 回调抛出，Agent.chat() 保留部分回复不回滚 |
| Core Memory | 核心记忆——从对话中提取的用户长期信息，存储在 core_memory.md + ChromaDB |
| BlockedError | 安全拦截异常——安全检测 BLOCK 时由 Agent.chat() 抛出，CLI/WS 各自捕获展示 |

---

# 文件结构速查

```
AI风堇_治愈晨昏/
├── main.py                          # CLI 入口：启动序列 + 对话循环 + 命令路由
├── start.bat                        # 一键启动脚本（双击启动后端+前端）
├── .env                             # API Key（不入 Git）
│
├── config/
│   ├── config.yaml                  # 主配置（Agent 参数）
│   ├── rag.yaml                     # RAG 策略参数
│   ├── context.yaml                 # 上下文窗口 + 记忆模板
│   ├── memory.yaml                  # 记忆存储/提取/合并
│   ├── safety.yaml                  # 安全检测配置
│   ├── safety_words/                # 安全词库（8 TXT + ~89 regex）
│   ├── mood.yaml                     # 情绪状态机配置（PAD/EMA/衰减/阈值/注入）
│   ├── system_prompt.md             # 风堇主人设
│   └── prompts/                     # Prompt 模板（core_memory / memory_extraction / memory_merge）
│
├── data/
│   ├── chroma/                      # RAG + Memory 共享向量库（Memory 已合并到此）
│   └── sessions/                    # 会话 JSON 文件
│
├── models/                          # 本地模型（自动 FP16 量化：bge-m3 ~2.1G/bge-reranker ~1.1G/Llama-Guard ~2.8G 磁盘）
├── logs/                             # app.log (Python全量) + renderer.log (前端)
│
├── src/
│   ├── config.py                    # Pydantic 配置模型（Config, RAGSettings, ContextSettings 等）
│   │
│   ├── agent/                       # Agent 核心（业务/对话编排层）
│   │   ├── core.py                  # Agent.chat() — 异步完整对话管线（安全→记忆→上下文→LLM→Tool→落盘），CLI/WS 唯一入口
│   │   ├── streaming.py             # stream_llm() — 纯 LLM 流式调用工具（零业务逻辑），CLI/WS 共用
│   │   ├── stream_controller.py     # StreamController — 流式取消标志 + 部分文本追踪
│   │   ├── context_manager.py       # ContextManager — 记忆注入 + 滑动窗口裁剪
│   │   ├── message_builder.py       # 共享消息组装（system_prompt + 回滚），CLI/WS 共用
│   │   ├── skill_registry.py        # SkillRegistry — 全局单例
│   │   ├── tool_registry.py         # ToolRegistry — 本地 + MCP 统一名称空间
│   │   ├── mcp_manager.py           # MCPManager — MCP 服务器生命周期
│   │   └── prompt_template.py       # Prompt 模板引擎
│   │
│   ├── capabilities/                # 能力基类
│   │   ├── skill.py                 # SkillBase（系统注入，LLM 不可见）
│   │   ├── tool.py                  # ToolBase（LLM 调用，返回 str）
│   │   └── mcp_server.py            # MCPServerBase（标准化工具协议）
│   │
│   ├── rag/                         # RAG 引擎（6 步管道）
│   │   ├── rag_service.py           # RAGService — 门面：retrieve() / ingest_document() / ingest_directory()
│   │   ├── a_loader.py ~ f_reranker.py    # Loader→Splitter→Indexer→Retriever→QueryEnhancer→Reranker
│   │   ├── embedding_registry.py    # 嵌入模型进程级单例（引用计数），RAG+Memory 共享 bge-m3
│   │   ├── chroma_registry.py       # ChromaDB 客户端进程级单例（引用计数），RAG+Memory 共享 PersistentClient
│   │   └── strategies/              # 策略仓库（splitter / index / retriever / query / reranker）
│   │
│   ├── memory/                      # 记忆系统
│   │   ├── manager.py               # MemoryManager — retrieve() / extract_async() / cleanup()
│   │   ├── extractor.py             # LLM 提取事实
│   │   ├── retriever.py             # 双层检索（Core 文件 + ChromaDB）
│   │   ├── storage.py               # ChromaDB 持久化
│   │   ├── writer.py                # 后台写入 + 三级路由 + 冲突消解
│   │   └── config.py                # MemorySettings
│   │
│   ├── mood/                        # 情绪状态机
│   │   └── engine.py                # MoodEngine — PAD+EMA+衰减+注入+持久化 (~120行)
│   │
│   ├── safety/                      # 安全护栏
│   │   ├── __init__.py              # SafetyManager — check(text) → SafetyResult
│   │   ├── rule_engine.py           # P0 规则引擎（SafetyConfig）
│   │   ├── guard_model.py           # P1 Llama Guard（GuardModelConfig）
│   │   └── loaders.py               # 规则/词库加载器
│   │
│   ├── session/                     # 会话管理
│   │   ├── session.py               # Session / Message / MessageMeta
│   │   ├── store.py                 # SessionStore — 原子 JSON 读写（.tmp → os.replace）
│   │   ├── manager.py               # SessionManager — CRUD + flush
│   │   └── context_restorer.py      # 上下文恢复
│   │
│   ├── mcp_servers/
│   │   └── rag_server.py            # RAGMCPServer — 暴露 rag_retrieve 工具
│   │
│   ├── skills/                      # Skill 实例（当前为空）
│   │
│   ├── server/                      # 服务器入口层（怎么起服务）
│   │   ├── app.py                   # create_app() FastAPI 工厂 + lifespan 单例加载（含 GPU 模型）
│   │   └── server.py                # uvicorn 启动入口（python -m src.server.server）
│   │
│   ├── ws/                          # WebSocket 传输适配层（瘦：只做协议，不含业务）
│   │   ├── connection.py            # /ws 端点 + 消息路由 + 报文映射，委托 Agent.chat()
│   │   └── schemas.py               # WS 协议 Pydantic 模型（ServerMessage/ClientMessage）
│   │
│   └── utils/
│       ├── logger.py                # loguru 配置
│       ├── helpers.py               # 通用工具
│       └── models.py                # 模型下载+FP16量化一体化（CLI/Server共用）
│
├── frontend/                        # 前端代码（Electron + TypeScript，尚未开发）
│   ├── src/
│   │   ├── main.ts                  # Electron 主进程入口
│   │   ├── preload.ts               # preload 脚本（IPC 桥接，暴露窗口控制 API）
│   │   └── renderer/
│   │       ├── index.html           # 入口 HTML
│   │       ├── main.ts              # 渲染进程入口，串联五大模块
│   │       ├── state.ts             # 中心状态管理（AppState）
│   │       ├── config.ts            # 前端配置中心（角色图/头像/WS地址/超时等）
│   │       ├── styles/
│   │       │   └── main.css         # 全局样式 + CSS 变量
│   │       ├── modules/
│   │       │   ├── character/
│   │       │   │   └── CharacterDisplay.ts  # 角色展示（图片加载 + 渐变背景 + 星光粒子）
│   │       │   ├── chat/
│   │       │   │   ├── ChatUI.ts            # 对话区 DOM 管理 + 滚动行为
│   │       │   │   ├── MessageRenderer.ts   # 消息气泡渲染（用户/AI/系统）
│   │       │   │   └── InputController.ts   # 输入框 + 发送/停止按钮逻辑
│   │       │   ├── sidebar/
│   │       │   │   └── HistorySidebar.ts    # 历史侧边栏（会话列表/切换/删除）
│   │       │   └── ws/
│   │       │       ├── WSClient.ts          # WebSocket 连接管理 + 心跳 + 超时
│   │       │       └── MessageParser.ts     # 消息解析 + 类型判断
│   │       ├── types/
│   │       │   └── protocol.ts      # WS 协议 TypeScript 类型定义
│   │       ├── utils/
│   │       │   └── dialog.ts        # 自定义确认弹窗（showConfirm）
│   ├── assets/
│   │   ├── fengjin.jpg              # 风堇角色展示图
│   │   ├── avatar-fengjin.png       # 风堇 AI 头像（对话区左侧圆形头像）
│   │   └── avatar-trailblazer.png   # 开拓者用户头像（对话区右侧圆形头像）
│   ├── electron-builder.yml         # 打包配置
│   ├── tsconfig.json
│   └── package.json
│
├── 学习_多轮cr经验.md               # 30 轮 CR 经验总结与开发规范启示
├── requirements.txt                 # Python 依赖
│
├── 核心文档/                        # 核心1(需求) 核心2(架构) 核心3(规范) 核心4(协议) + CR流程
├── 重要文档/                        # 通用 CR 流程 / 可复用开发规范
└── 前端开发核心文档/                 # 前端详细方案文档 1-3（实现后归档）
```

---

# 红线速查

违反以下任何一条即出严重问题。

## 架构红线

1. **禁止引入 LangChain / LlamaIndex / LangGraph 等框架作为骨架**。原子工具库（openai, chromadb, sentence-transformers, rank_bm25）允许直接调用。
2. **新增依赖必须经用户明确许可**。
3. **禁止硬编码**——配置、路径、常量、魔法数字必须通过配置文件或模块级常量定义。
4. **模块无循环依赖**——核心 Agent 不依赖具体 Skill，各模块单向依赖。

## 数据安全与护栏红线

5. **API 密钥禁止写入文件**——只能通过环境变量（`FENGJIN_*`、`MEMO_*`）读取。
6. **日志中禁止打印 API Key、Token 的实际值**。
7. **会话文件必须原子写入**——先写 `.json.tmp` 再 `os.replace()`。任何持久化写入都需考虑中途崩溃。
8. **静默失败零容忍**——空 `except` 或 `except Exception` 吞异常时必须记录 `logger.error()`。关键操作失败（会话保存、记忆写入、RAG 索引）必须产生用户可见提示或至少 ERROR 级别日志。
9. **loguru 日志禁止 f-string 预插值**——loguru 会把第一个字符串参数当格式串再次解析。异常对象 `e`、用户输入、JSON 字符串中的 `{...}` 会被当成占位符，触发 `KeyError` 导致日志调用自身崩溃、掩盖真实错误。必须用 `logger.error("描述: {}", e)`（loguru 原生格式化，变量值不二次解析），禁止 `logger.error(f"描述: {e}")`。
10. **宁可漏拦不误拦**——安全规则的精确定义优先于覆盖范围。正常对话被误拦比漏拦更影响体验。
11. **Self-harm 用 Comfort 而非 Block**——自杀/自伤内容不直接拦截，改为注入安抚指令。

## 资源红线

12. **加载到 GPU 的模型必须有 cleanup()**——需同时做 `self._model = None`（当前全部 CPU 模式，仅 guard_model 保留空缓存调用）。
13. **不能同时驻留超过显存容量的模型**——当前预算（全部 FP16，CPU 模式）：bge-m3 ~550MB + bge-reranker-v2-m3 ~550MB + Llama-Guard-3-1B ~2GB（默认关闭，env var 控制）= 基础 ~1.1GB / 全开 ~3.1GB。
14. **ChromaDB PersistentClient 必须走 chroma_registry.acquire() 共享单例**——禁止各模块独立创建客户端。Memory 和 RAG 已合并到 data/chroma 同目录不同 collection。
15. **新增本地模型必须走 ensure_models() 统一下载+FP16 量化**——禁止自行下载或加载原始精度。src/utils/models.py 是模型获取唯一入口。
16. **ChromaDB PersistentClient 在 cleanup 中必须关闭或置 None**。
17. **daemon 线程必须有停止信号和 join 超时**。
18. **`cleanup()` 必须是幂等的**——加 `self._cleaned` 标志位，支持 cleanup→reinit→cleanup 序列。`initialize()` 中必须将 `_cleaned` 重置为 `False`。
19. **持有资源的 `__init__`/`initialize()` 必须支持部分初始化回滚**——中途失败时清理已初始化的子组件，防止 GPU 模型/ChromaDB/线程永久泄漏。

## Python 陷阱红线

18. **禁止 `from module import 可变变量`**——Python 的 `from X import Y` 将 Y 的当前值绑定到本地名称空间，Y 被重新赋值后本地绑定不会更新。必须用 `from ... import module as alias` 然后 `alias.variable` 动态访问。
19. **所有文件路径必须以 `Path(__file__).resolve()` 为基准计算绝对路径**——禁止依赖工作目录的相对路径。路径计算统一模式：`_root = Path(__file__).resolve().parent.parent.parent`（`src/` → 项目根）。

---

# 技术约束

## 依赖决策

引入新依赖前按此顺序判断：① 标准库能否解决？→ ② 已有依赖能否解决？→ ③ 是否只用到原子工具函数？→ ④ 是否引入框架包装？→ ④即禁止。

## 双模型架构

- 主对话模型：`FENGJIN_API_KEY` / `FENGJIN_BASE_URL` / `FENGJIN_MODEL`
- 记忆小模型：`MEMO_API_KEY` / `MEMO_BASE_URL` / `MEMO_MODEL`
- 记忆提取用小模型降成本，主对话用大模型保质量

## 三种能力模型

| 能力 | 基类 | 触发者 | LLM 可见 | 用途 |
|------|------|--------|-----------|------|
| Skill | SkillBase | 系统代码 | 否 | 提示词注入（系统决定时机），延迟初始化 |
| Tool | ToolBase | LLM | 是 | 函数调用（LLM 自主决定），返回 str |
| MCP | MCPServerBase | LLM | 是 | 标准化工具协议，注册时立即初始化 |

## 关键约束

- **Tool Calling 上限 = 5**（从 `config.agent.max_tool_rounds` 读取），防止无限递归
- **RAG 检索结果硬截断 1500 字符**，防止挤占对话 token
- **滑动窗口 = 25 轮（50 条消息）**（从 `context.yaml` 读取），双重保护（轮数 + Token 估算）
- **超长输入 >10000 字符** 拒绝并提示
- **ChromaDB 非线程安全**，所有写入通过单线程 Queue 串行化
- **知识库内容限定翁法罗斯世界观**，不包含现实世界专属知识
- **记忆注入到用户消息中（非 system_prompt）**，增强版仅当轮使用不入历史
- **Skill 执行优先于记忆注入**——`chat()` 先 Skill 后记忆
- **安全检测在 Agent.chat() 内部**——`SafetyManager.check()` 是管线第一步（BLOCK→BlockedError / COMFORT→安抚注入 / PASS→继续）

## 清理链

启动：Mood → Memory → Context → Agent → RAG → MCP → Safety → Session

退出：Session.flush() → Agent.cleanup()（含 Skill+MCP+Tool）→ Memory.cleanup()（含 writer.stop()+storage）→ Mood.cleanup() → RAG.cleanup()（reranker→query_enhancer→retriever→indexer→splitter→loader）→ Safety.cleanup() → logger.complete()

---

# 代码规范要点

只列本项目特有的、AI 不会自然注意到的规则。

- **配置**：所有配置走 YAML + Pydantic，每个字段有默认回退。system_prompt 外置 `.md` 文件。
- **导入**：标准库 → 第三方库 → 本地模块，本地用相对导入 `from .module import Class`。
- **类型**：函数必须有类型注解。类和公共函数写 docstring。
- **日志**：用 loguru。每次对话生成 `trace_id`。框架入口统一记日志（Agent.chat、SkillRegistry.execute），业务代码保持干净。临时调试日志用完即删，用 `# TEMP:` 标记。
- **函数长度**：不超过 30 行。
- **新增功能检查清单**：
  1. 是否新增依赖？→ 红线第 2 条
  2. 是否新增配置项？→ 对应 YAML + Pydantic
  3. 是否持有 GPU？→ 必须有 cleanup()
  4. 是否持有 ChromaDB？→ cleanup() 中关闭
  5. 是否新增环境变量？→ 更新 `.env.example`
  6. 知识库内容是否限定翁法罗斯世界观？
