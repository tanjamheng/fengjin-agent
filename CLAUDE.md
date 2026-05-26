# AI风堇立项书

愿这一抹微光，治愈晨昏！

## 项目愿景

制作游戏AI NPC，还原《崩坏：星穹铁道》中的风堇，与我对话，治愈我。
我会永远维护这个项目，永远优化下去，让ai越来越还原风堇。

## 迭代目标

1. AI能够越来越还原风堇，甚至还原三观性格。
2. 降低出错率，穿帮率。
3. 前端风堇动画尽可能唯美。

# 技术架构文档

## 前言
 - 本项目前端用typescript，后端用python。

## 1. 前端侧

### 对话窗口

- 作用：用户输入文字、展示风堇回复。
- 目标：流式输出，文字逐字呈现，配合动作表情形成自然对话感。
- 对接：接收核心对话侧返回的流式文本。
- 对外影响：流式延迟直接影响用户体感，需与后端流式推理管道匹配。

### 风堇建模 / 动作 / 表情

- 作用：3D 或 Live2D 风堇形象，执行动作和表情。
- 目标：根据对话内容做出相应神态和肢体动作，提升沉浸感。
- 对接：接收 mcp/tool 模块下发的动作表情指令。
- 注意点：动作与文本的时序配合是关键——先触发动作再流式吐字，还是同步，需要实际调。

### 小伊卡防护动画

- 作用：当用户输入被小伊卡拦截时播放的视觉反馈。
- 目标：用角色内方式（小伊卡出场提醒）软性拒绝，不破坏沉浸感。
- 对接：小伊卡安全检测模块触发。

## 2. 核心对话侧

### 小伊卡安全检测

- 作用：用小模型判断用户输入是否包含道德问题、不尊重风堇的内容。
- 目标：在对话进入主流程前拦截恶意输入，保护角色边界。
- 对接：前端输入先进此处，通过则交给 context_manage，拦截则触发前端小伊卡动画。
- 注意点：小模型的延迟需控制，避免成为对话响应瓶颈。

### context_manage（多轮对话上下文管理）

- 作用：管理当前会话的多轮对话历史，控制送入 LLM 的上下文窗口。
- 目标：让风堇能记住这一轮在聊什么，不会前言不搭后语。
- 对接：串联 system prompt、memory、rag、mcp/tool、LLM，是核心调度点。
- 对外影响：窗口过长 token 成本高、响应慢；过短则丢上下文。需要权衡策略。

### system prompt

- 作用：定义"我是风堇"，承载她的性格、身份、爱好、能力、技能、说话风格、人物关系。
- 目标：让 LLM 的输出始终锚定在风堇的人设上。

### memory

- 作用：记住灰宝的爱好等个性化信息。
- 目标：让风堇能跨会话记住用户，形成长期陪伴感。
- 对接：context_manage 在对话开始时读取相关记忆，在对话结束后写入新的记忆点。
- 注意点：记忆的提取时机和存储结构需要定义清楚，避免记错或记杂。记忆是一个单独的系统，且日后需要大量迭代优化和扩展。

### RAG

- 作用：检索风堇知识库中的相关内容，注入对话。
- 目标：让风堇了解翁法罗斯、自身设定，不知道现实世界专属知识，降低穿帮。
- 对接：context_manage 在需要补充知识时调用，底层连接数据侧的知识库。

### mcp / tool

- 作用：调用前端风堇的动作和表情。
- 目标：让语言和神态同步，增强"活人感"。
- 对接：LLM 回复中需要能携带动作指令，context_manage 解析后下发前端
- 注意点：动作指令的格式（隐式标记还是显式 tool call）需要约定。

### 大语言模型 API

- 作用：核心推理引擎。
- 目标：可配置 apikey 和 baseurl，支持灵活切换模型，方便调试和升级。
- 对接：由 context_manage 组装 prompt 后调用。

## 3. 数据侧

### 风堇知识库

- 作用：分类管理风堇相关知识，作为 RAG 的检索源。
- 目标：在持续迭代中越来越全面，覆盖翁法罗斯背景、角色关系、台词、事件等。
- 对接：被 RAG 模块检索使用。

## 4. 评估侧

### OOC 率评估

- 作用：衡量风堇回复偏离人设的程度。
- 目标：持续降低 OOC 率，保持角色一致性。
- 对接：对 LLM 输出进行抽检或自动评判。

### 性能评估

- 作用：衡量对话响应延迟。
- 目标：保证从用户输入到前端开始流式输出的时间在可接受范围。
- 对接：关注 context_manage 到 LLM 返回首 token 的全链路延迟。

### 安全评估

- 作用：衡量小伊卡安全检测的拦截效果。
- 目标：漏拦率和误拦率都控制在理想范围。
- 对接：对小伊卡模块进行测试评估。

## 5. 未来扩展（暂不实现，预留方向）

- 后处理规则重写：在 LLM 输出后做文本级修正。
- logit_bias：在模型推理层面对特定 token 做概率压制。
- 状态机：代码层面分情况控制风堇语气和情绪状态。
- 大模型微调和本地部署：进一步提升风格还原度和降低延迟。

# 核心旅程：
用户输入->前端对话框->小伊卡安全检测模块->核心大模型侧(加载模型system prompt,mcp,tool,skill等)->context_manage多轮对话管理->rag+memory->回复->前端流式输出

# 文件结构总览参考
```
AI风堇_治愈晨昏/
│
├── main.py                          # CLI 入口（当前已有，逐步演进）
├── requirements.txt                 # 依赖管理（未来迁移到 pyproject.toml）
├── .env                             # API Key 等敏感信息（不提交 Git）
├── .gitignore
├── README.md
├── CLAUDE.md                        # Claude Code 指令
│
├── config/                          # 【配置层】所有配置文件
│   ├── config.yaml                  # 主配置（Agent 参数）
│   ├── config.example.yaml          # 示例配置
│   ├── rag.yaml                     # RAG 专用配置
│   ├── safety.yaml                  # 安全检测配置（新增）
│   └── prompts/                     # Prompt 模板文件（新增）
│       ├── system_prompt.md         # 风堇主人设
│       ├── personality.md           # 性格细节
│       ├── knowledge_boundary.md    # 知识边界定义
│       ├── relationships.md         # 人物关系
│       └── safety_rules.md          # 安全规则
│
├── data/                            # 【数据层】运行时数据
│   ├── chroma/                      # ChromaDB 向量数据（已存在）
│   ├── memory/                      # 记忆系统数据（新增）
│   │   └── user_profile.db          # SQLite 用户画像
│   └── knowledge/                   # 知识库原始文件（新增）
│       ├── 角色设定/                 # 风堇基础设定
│       ├── 世界观/                   # 翁法罗斯、星铁背景
│       ├── 台词/                     # 风堇经典台词
│       ├── 人物关系/                 # 与其他角色的关系
│       └── 剧情事件/                 # 主线/活动剧情
│
├── logs/                            # 【日志层】运行时日志（已存在）
│
├── src/                             # 【业务代码层】核心实现
│   ├── __init__.py
│   ├── config.py                    # 配置管理（已有，需扩展）
│   ├── exceptions.py                # 全局异常定义（新增）
│   │
│   ├── capabilities/                # 【能力基类】Skill/Tool/MCP 三种能力的抽象定义
│   │   ├── __init__.py
│   │   ├── skill.py                 # Skill 基类（提示词模版，系统决定注入时机）
│   │   ├── tool.py                  # Tool 基类（函数调用，LLM 自主决定）
│   │   └── mcp_server.py            # MCP 服务器基类（标准化工具协议）
│   │
│   ├── agent/                       # 【Agent 核心】
│   │   ├── __init__.py
│   │   ├── core.py                  # Agent 核心类（支持 Skill/Tool/MCP + tool calling 循环）
│   │   ├── skill_registry.py        # Skill 注册中心
│   │   ├── tool_registry.py         # Tool 注册中心（管理本地 Tool + MCP Tool）
│   │   ├── mcp_manager.py           # MCP 管理器（管理 MCP 服务器生命周期）
│   │   ├── prompt_template.py       # Prompt 模板管理
│   │   ├── orchestrator.py          # 对话编排器（新增）
│   │   └── context_manager.py       # 多轮上下文管理（新增）
│   │
│   ├── mcp_servers/                 # 【MCP 服务器实例】
│   │   ├── __init__.py
│   │   └── rag_server.py            # RAG MCP 服务器（封装 RAGService，暴露 rag_retrieve 工具）
│   │
│   ├── skills/                      # 【Skill 实例】可插拔技能（基类在 capabilities/）
│   │   ├── __init__.py
│   │   ├── safety_skill.py          # 小伊卡安全检测（新增）
│   │   ├── memory_skill.py          # 记忆技能（新增）
│   │   └── action_skill.py          # 动作/表情技能（新增，未来）
│   │
│   ├── rag/                         # 【RAG 引擎】（已有，结构完整）
│   │   ├── __init__.py
│   │   ├── a_loader.py              # 文档加载
│   │   ├── b_splitter.py            # 文本切分
│   │   ├── c_indexer.py             # 索引构建
│   │   ├── d_retriever.py           # 检索
│   │   ├── e_query_enhancer.py      # 查询增强
│   │   ├── f_reranker.py            # 重排序
│   │   ├── rag_service.py           # RAG 服务层（纯功能，供 MCP/直接调用）
│   │   └── strategies/              # 策略仓库
│   │       ├── splitter/
│   │       ├── index/
│   │       ├── retriever/
│   │       ├── query/
│   │       └── reranker/
│   │
│   ├── memory/                      # 【记忆系统】（新增模块）
│   │   ├── __init__.py
│   │   ├── base.py                  # 记忆抽象接口
│   │   ├── mem0_adapter.py          # Mem0 适配层（封装 Mem0 SDK）
│   │   ├── memory_manager.py        # 记忆管理器（读写调度）
│   │   └── fengjin_filter.py        # 风堇专用记忆过滤器（未来增强）
│   │
│   ├── safety/                      # 【安全系统】（新增模块）
│   │   ├── __init__.py
│   │   ├── base.py                  # 安全检测抽象接口
│   │   ├── rule_filter.py           # 规则引擎（关键词+正则）
│   │   ├── prompt_guard.py          # Prompt-Guard-86M 检测（Phase 2）
│   │   └── safety_manager.py        # 安全管理器（分层调度）
│   │
│   ├── prompt/                      # 【Prompt 管理】（新增模块）
│   │   ├── __init__.py
│   │   ├── renderer.py              # 模板渲染（Jinja2）
│   │   └── loader.py                # 模板加载器
│   │
│   ├── eval/                        # 【评估系统】（新增模块）
│   │   ├── __init__.py
│   │   ├── ooc_evaluator.py         # OOC 率评估
│   │   ├── safety_evaluator.py      # 安全评估
│   │   ├── performance_evaluator.py # 性能评估
│   │   ├── rag_evaluator.py         # RAG 效果评估
│   │   ├── memory_evaluator.py      # 记忆效果评估
│   │   └── report.py                # 评估报告生成
│   │
│   ├── api/                         # 【API 层】（未来，前端时启用）
│   │   ├── __init__.py
│   │   ├── app.py                   # FastAPI 应用
│   │   ├── routes/
│   │   │   ├── chat.py              # 对话接口
│   │   │   ├── ingest.py            # 知识库管理接口
│   │   │   └── memory.py            # 记忆管理接口
│   │   └── websocket.py             # WebSocket 流式接口
│   │
│   └── utils/                       # 【工具层】（已有）
│       ├── __init__.py              # 已有
│       ├── logger.py                # 日志系统（已有）
│       ├── helpers.py               # 工具函数（已有）
│       └── text_utils.py            # 文本处理工具（新增）
│
├── tests/                           # 【测试层】验证代码能跑通、思路可行
│   ├── unit/                        # 单元测试（函数级正确性）
│   └── integration/                 # 集成测试（模块间连通性、最小可行性验证）
│
├── evaluate/                        # 【评估层】调用子功能并量化测评效果
│   ├── rag/                         # RAG 检索质量评估（召回率、相关性评分）
│   ├── ooc/                         # 角色一致性评估（OOC 率、风格偏离度）
│   ├── safety/                      # 安全检测评估（漏拦率、误拦率）
│   └── performance/                 # 性能评估（延迟、吞吐量）
│
├── scripts/                         # 【脚本层】
│   ├── rag_evaluation_pipeline.py   # 已有
│   ├── build_knowledge.py           # 知识库构建脚本（新增）
│   ├── eval_ooc.py                  # OOC 评估脚本（新增）
│   └── eval_rag.py                  # RAG 评估脚本（新增）
│
├── 核心文档/                        # 【项目文档】
│   ├── 1.AI风堇立项书.md
│   ├── 2.AI风堇项目规划.md
│   ├── 3.技术架构文档.md
│   ├── 4.核心旅程.md
│   ├── 5.开发计划.md
│   ├── 6.评估体系文档.md
│   ├── 7.技术选型.md
│   └── 8.文件结构规划.md
│
├── 数据侧_风堇资料/                 # 【数据侧】原始资料收集
│
└── 学习文档/                        # 学习笔记（开发参考）
    ├── 学习_RAG模块.md
    ├── 学习_RAG评估.md
    └── ...
```

# tests 与 evaluate 的区别

这两个文件夹用途完全不同，放错地方的脚本需要迁移：

## tests/ — 验证"能不能跑"

- **目的**：确认代码逻辑正确、模块间能连通、某个思路可行
- **特点**：最小脚本、快速验证、不关注效果好坏、assert 通过即可
- **适合放入**：
  - 单元测试（函数输入输出是否正确）
  - 集成测试（模块 A 调模块 B 能否跑通）
  - 可行性验证脚本（试某新库 API、验证某个算法思路）
  - diagnose 脚本（诊断报错、排查问题）
- **命名**：`test_*.py` 或 `diagnose_*.py`
- **运行方式**：`pytest tests/` 或直接 `python tests/xxx.py`

## evaluate/ — 量化"效果好不好"

- **目的**：调用 agent 的子功能，用测评工具和指标量化效果
- **特点**：需要准备测试数据集、跑完整流程、输出评分/报告、可重复对比
- **适合放入**：
  - RAG 检索质量评估（召回率、MRR、相关性评分）
  - 角色一致性评估（OOC 率、风格偏离度打分）
  - 安全检测评估（漏拦率、误拦率统计）
  - 性能基准测试（端到端延迟、首 token 时间）
  - A/B 对比实验（不同参数/策略的效果对比）
  - 评估数据集和评估报告
- **命名**：按测评维度分目录，如 `evaluate/rag/`、`evaluate/ooc/`
- **运行方式**：`python evaluate/rag/eval_recall.py`

## 判断规则

| 问题 | 放哪 |
|------|------|
| "这个函数能不能跑通？" | tests/ |
| "这两个模块连起来会不会报错？" | tests/ |
| "这个新库的 API 怎么用？" | tests/ |
| "RAG 召回了多少条相关结果？" | evaluate/ |
| "风堇这 10 句话有几句 OOC 了？" | evaluate/ |
| "换了个参数，效果变好了吗？" | evaluate/ |

---

# 个人开发计划
数据侧->核心对话侧->评估侧->前端侧

核心对话侧：

agent框架->调用api->system_prompt->rag->memory->context_manage->小伊卡安全检测

->评估：性能，安全，OOC率,RAG和memory召回率

->前端：对话界面->风堇动画->mcp调用风堇表情

# 当前正在进行的核心任务
优化rag

# 可用的 Subagent 团队

| Agent | 用途 | 调用时机 |
|-------|------|----------|
| `team-lead` | 任务统筹、进度管理 | 复杂多任务项目 |
| `architecture-designer` | 系统架构设计 | 新项目启动、技术选型 |
| `backend-developer` | 后端API开发 | API实现、数据库设计 |
| `frontend-developer` | 前端界面开发 | UI实现、组件开发 |
| `security-auditor` | 安全审查 | 代码完成后检查漏洞 |
| `test-engineer` | 测试编写执行 | 功能完成后编写测试 |

---

# 技术选型契约

本项目的核心原则：**手搓为主，工具箱为辅**。这是个人练手+开源项目，学习价值 > 开发速度。

## 框架红线（禁止）

以下行为**未经用户明确许可，一律禁止**：

1. **禁止引入重量级 AI 框架作为脚手架** — 不得使用 LangChain Agent/Chain/Memory、LangGraph、LlamaIndex、AutoGen、CrewAI 等框架来构建本项目的 Agent 核心、对话编排、记忆系统。这些模块必须手搓。
2. **禁止用框架包装已有的手搓模块** — 不得将现有手搓的 RAG、Skill、Memory 等模块迁移或适配到某个框架的抽象层上。
3. **禁止为了"方便"引入不必要的依赖** — 每新增一个第三方库都必须有充分理由，不能因为"这个库也能做"就引入。

## 允许使用的工具库（工具箱原则）

可以用**原子级**的第三方工具库解决具体问题，但不能让它们成为项目骨架：

| 类别 | 允许 | 说明 |
|------|------|------|
| LLM 调用 | `anthropic` SDK、`openai` SDK | 直接调用 API，不经过框架包装 |
| 向量数据库 | `chromadb` | 直接使用，不用 LangChain VectorStore 包装 |
| 文本处理 | `langchain-text-splitters` 的单个切分器 | 只用原子工具函数，不用 Chain/Agent |
| 配置 | `pydantic`、`pyyaml` | 数据校验和配置解析 |
| 日志 | `loguru` | 统一日志 |
| Web 框架 | `fastapi`（未来前端阶段） | API 层 |
| 模板 | `jinja2` | Prompt 模板渲染 |
| 稀疏检索 | `rank_bm25` | BM25 算法 |
| 文档解析 | `pypdf`、`python-docx` | 文件格式处理 |

## 新增依赖的决策规则

引入任何新依赖前，必须按以下顺序判断：

1. **能否用 Python 标准库解决？** → 能就用标准库
2. **能否用已有依赖解决？** → 能就用已有的
3. **是否只用到目标库的原子工具函数？** → 允许引入，但只 import 需要的部分
4. **是否需要引入一个框架来"编排"或"包装"现有代码？** → 禁止，必须手搓

## 项目定位备忘

- 这是**个人练手项目**，学习 AI 工程全链路是核心目标
- 这是**开源项目**，手搓框架比框架 wrapper 更有辨识度
- 项目的独特价值在于**风堇角色还原**，不在于技术栈先进性
- 用 LangChain 当工具箱（借用原子工具），不当脚手架（不用框架架构）

---

# 代码规范

## 基本原则
1. **禁止硬编码** - 配置、路径、常量都要通过配置文件或常量定义。本项目长期维护，硬编码会在后续迭代中埋雷踩坑。只有满足以下全部条件时才允许硬编码：变量值足够简单、与业务逻辑无耦合、后续几乎不可能变动。一切文件列表、跳过规则、分类映射等维护性内容，必须放在配置文件中。
2. **模块独立** - 核心 Agent 不依赖具体 Skill，各模块无循环依赖
3. **单一职责** - 一个函数只做一件事，不超过 30 行

## 命名规范
- 函数/变量：`snake_case`，如 `load_config()`、`user_input`
- 类名：`PascalCase`，如 `SkillBase`、`RAGConfig`
- 常量：`UPPER_CASE`，如 `MAX_TOKENS`、`DEFAULT_TIMEOUT`
- 私有属性/方法：前缀 `_`，如 `_internal_state`
- 命名要有意义，禁止 `tmp`、`data`、`x` 等无意义名称

## 导入规范
```python
# 顺序：标准库 → 第三方库 → 本地模块，每组之间空一行
import os
import sys

from anthropic import Anthropic
from pydantic import BaseModel

from src.config import Config
from src.utils.logger import get_logger
```
- 禁止 `from module import *`
- 本地模块用相对导入 `from .module import Class`

## 类型与注释
- 函数必须有类型注解：`def execute(self, input: str) -> dict:`
- 复杂逻辑写简短注释，简单逻辑不写
- 类和公共函数写 docstring（一句话说明功能）

## 错误处理
- 禁止空 `except:` 或 `except Exception:` 吞掉所有异常
- 捕获具体异常：`except FileNotFoundError:`
- 错误必须用 logger 记录，不要只 print

## 配置与敏感信息
- 配置统一放 `config/*.yaml`
- API Key 等敏感信息用环境变量，禁止写入文件
- 使用 Pydantic 校验配置

## 日志规范
- 用 `loguru`，分级：DEBUG / INFO / WARNING / ERROR
- 每次请求生成 `trace_id` 串联日志
- 敏感信息脱敏

**日志简洁原则**：
- 框架入口统一记录（Agent.chat、SkillRegistry.execute），业务代码保持干净
- 只在关键业务节点写少量log（文档导入成功、检索结果数等）
- 内部逻辑不写log（数据处理、字符串拼接等）
- 临时调试log用完即删，用 `# TEMP:` 或 `# DEBUG:` 标记便于识别
