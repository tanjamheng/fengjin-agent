# AI风堇立项书

愿这一抹微光，治愈晨昏！

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
这三个文档将是开发的最高宗旨，不容违反。一切开发将以其为锚点。
其中的核心内容已写入CLAUDE.md中，如有必要，可以进行复习这些文档中的内容。
每当开发新功能时，也会将新功能相关的需求，技术架构等写入这些核心文档中。

---

# 功能速查

## 核心链路

用户输入 → 小伊卡安全检测（P0规则 + P1 Llama Guard）→ 多轮上下文组装（记忆注入 + 滑动窗口裁剪）→ LLM 流式生成（支持 Tool Calling 自主检索知识库，最多 5 轮）→ 流式输出 → 异步记忆提取 → 会话持久化

## 后端已有能力

| 模块 | 做了什么 |
|------|---------|
| 对话引擎 | 流式对话 + Tool Calling 循环 + 停止/超时处理 |
| 风堇角色系统 | 外部 system_prompt.md 定义人设，调角色不改代码 |
| RAG 知识库 | 6 步管道检索风堇相关知识，LLM 自主决定调用时机 |
| 记忆系统 | 跨会话记住用户信息，双存储（core_memory.md + ChromaDB），异步提取 |
| 安全护栏 | 两级检测（规则引擎 + Llama Guard 3 1B），11 类拦截，Comfort 安抚模式 |
| 会话管理 | JSON 原子写入，14 个 CLI 命令（含会话、知识库管理、调试） |
| WebSocket API | FastAPI + /ws 端点，流式推送 + 取消控制（前端联调用） |

## 前端（V1 计划中，尚未开发）

Electron 桌面客户端，三栏布局（角色展示 38% + 对话区 42% + 历史侧边栏 20%），WebSocket 通信，流式打字效果。

---

# 文件结构速查

```
AI风堇_治愈晨昏/
├── main.py                          # CLI 入口：启动序列 + 对话循环 + 命令路由
├── .env                             # API Key（不入 Git）
│
├── config/
│   ├── config.yaml                  # 主配置（Agent 参数）
│   ├── rag.yaml                     # RAG 策略参数
│   ├── context.yaml                 # 上下文窗口 + 记忆模板
│   ├── memory.yaml                  # 记忆存储/提取/合并
│   ├── safety.yaml                  # 安全检测配置
│   ├── safety_words/                # 安全词库（8 TXT + ~89 regex）
│   ├── system_prompt.md             # 风堇主人设
│   └── prompts/                     # Prompt 模板（core_memory / memory_extraction / memory_merge）
│
├── data/
│   ├── chroma/                      # RAG 向量库
│   ├── memory_chroma/               # 记忆向量库
│   └── sessions/                    # 会话 JSON 文件
│
├── models/                          # 本地模型（bge-m3 / bge-reranker-v2-m3 / Llama-Guard-3-1B）
├── logs/
│
├── src/
│   ├── config.py                    # Pydantic 配置模型（Config, RAGSettings, ContextSettings 等）
│   │
│   ├── agent/                       # Agent 核心（业务/对话编排层）
│   │   ├── core.py                  # Agent.chat() — 同步流式调用 + tool calling 循环（CLI）
│   │   ├── streaming.py             # stream_reply() — 流式对话 service 层（安全→上下文→LLM→取消），WS/CLI 共用
│   │   ├── stream_controller.py     # StreamController — 流式取消标志 + 部分文本追踪
│   │   ├── context_manager.py       # ContextManager — 记忆注入 + 滑动窗口裁剪
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
│   │   ├── connection.py            # /ws 端点 + 消息路由 + 报文映射，委托 agent/streaming
│   │   └── schemas.py               # WS 协议 Pydantic 模型（ServerMessage/ClientMessage）
│   │
│   └── utils/
│       ├── logger.py                # loguru 配置
│       └── helpers.py               # 通用工具
│
├── 核心文档/                        # 需求梳理 / 技术架构 / 开发规范
└── 前端开发核心文档/                 # 前端 5 文档
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
9. **宁可漏拦不误拦**——安全规则的精确定义优先于覆盖范围。正常对话被误拦比漏拦更影响体验。
10. **Self-harm 用 Comfort 而非 Block**——自杀/自伤内容不直接拦截，改为注入安抚指令。

## 资源红线

11. **加载到 GPU 的模型必须有 cleanup()**——需同时做 `self._model = None` + `torch.cuda.empty_cache()`。
12. **不能同时驻留超过显存容量的模型**——当前预算：bge-m3 ~1.1GB + bge-reranker-v2-m3 ~1.1GB + Llama-Guard-3-1B ~2GB = ~4.2GB。
13. **ChromaDB PersistentClient 在 cleanup 中必须关闭或置 None**。
14. **daemon 线程必须有停止信号和 join 超时**。

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
- **滑动窗口 = 20 轮（40 条消息）**（从 `context.yaml` 读取），双重保护（轮数 + Token 估算）
- **超长输入 >10000 字符** 拒绝并提示
- **ChromaDB 非线程安全**，所有写入通过单线程 Queue 串行化
- **知识库内容限定翁法罗斯世界观**，不包含现实世界专属知识
- **记忆注入到用户消息中（非 system_prompt）**，增强版仅当轮使用不入历史
- **Skill 执行优先于记忆注入**——`chat()` 先 Skill 后记忆
- **安全检测在 Agent 外部**——`SafetyManager.check()` 在 `Agent.chat()` 之前

## 清理链

启动：Memory → Context → Agent → RAG → MCP → Safety → Session

退出：Session.flush() → Agent.cleanup()（含 Skill+MCP+Tool）→ Memory.cleanup()（含 writer.stop()+storage）→ RAG.cleanup()（Reranker→Retriever→Indexer）→ Safety.cleanup() → logger.complete()

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
