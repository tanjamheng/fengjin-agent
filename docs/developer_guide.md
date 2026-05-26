# 开发者指南：扩展须知与规范

本文档面向希望在本项目基础上进行二次开发的开发者，涵盖架构概览、扩展方式、代码规范和实战示例。

---

## 1 架构概览

本项目采用**核心 + 插件**的分层架构：

```
┌─────────────────────────────────────────────────┐
│                    main.py                       │  CLI 入口
│          （创建 Agent、注册 Skill、命令循环）        │
├─────────────────────────────────────────────────┤
│                   Agent 核心层                    │
│  ┌──────────┐  ┌────────────────┐  ┌─────────┐ │
│  │   core   │──│ skill_registry │──│  prompt  │ │
│  │ (对话引擎) │  │  (Skill 注册表) │  │ template│ │
│  └──────────┘  └────────────────┘  └─────────┘ │
├─────────────────────────────────────────────────┤
│                   Skill 层                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │   RAG    │  │  你的     │  │  你的     │      │
│  │  Skill   │  │  Skill   │  │  Skill   │ ...  │
│  └──────────┘  └──────────┘  └──────────┘      │
├─────────────────────────────────────────────────┤
│                  基础设施层                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │  config  │  │  logger  │  │ helpers  │      │
│  │ (Pydantic)│  │ (loguru) │  │ (路径等)  │      │
│  └──────────┘  └──────────┘  └──────────┘      │
└─────────────────────────────────────────────────┘
```

### 1.1 核心设计原则

1. **Agent 核心纯净**：`Agent` 类（`src/agent/core.py`）不依赖任何具体 Skill，所有功能通过 `SkillRegistry` 动态扩展。
2. **Skill 即插即用**：每个 Skill 继承 `SkillBase`，实现 `execute()` 方法即可，通过 `agent.register_skill()` 注册。
3. **策略模式**：RAG 管线的每个阶段（分块、索引、检索、重排序、查询增强）都采用策略模式，可独立替换。

### 1.2 关键类的职责

| 类 | 文件 | 职责 |
|----|------|------|
| `Agent` | `src/agent/core.py` | 对话引擎，持有 API Client、消息历史、SkillRegistry 引用 |
| `SkillRegistry` | `src/agent/skill_registry.py` | 单例注册表，管理 Skill 的注册/发现/执行/生命周期 |
| `SkillBase` | `src/skills/base.py` | Skill 抽象基类，定义 `initialize/execute/cleanup` 生命周期 |
| `SkillContext` | `src/skills/base.py` | 执行上下文：trace_id、用户输入、对话历史、配置 |
| `SkillResult` | `src/skills/base.py` | 执行结果：成功/失败、数据、消息、错误信息 |
| `SkillMeta` | `src/skills/base.py` | 元信息：名称、描述、版本、依赖、作者 |
| `PromptManager` | `src/agent/prompt_template.py` | 模板管理器，注册和渲染 Prompt 模板 |
| `Config` | `src/config.py` | 全局配置，Pydantic 校验，API Key 从环境变量读取 |

### 1.3 数据流

```
用户输入
  │
  ├─ 是命令？ ──→ main.py 处理（/ingest, /stats 等）
  │
  └─ 普通消息 ──→ agent.chat(input, skills=["rag"])
                     │
                     ├─ 构建 SkillContext
                     │
                     ├─ 遍历 skills 列表，逐一调用
                     │   registry.execute(name, context)
                     │     │
                     │     ├─ 如果 Skill 未初始化，先调用 initialize()
                     │     ├─ 调用 skill.execute(context) → SkillResult
                     │     └─ 如果 result.data["prompt"] 存在，
                     │        用它替换原始用户输入
                     │
                     ├─ 将处理后的 prompt 添加到消息历史
                     │
                     ├─ 调用 Anthropic API 获取回复
                     │
                     └─ 返回回复文本
```

---

## 2 扩展方式一：开发新 Skill

这是最常见的扩展方式。所有新功能都应该封装为 Skill。

### 2.1 Skill 生命周期

```
注册(register) → 初始化(initialize) → 执行(execute) × N → 清理(cleanup)
```

- **注册**：在 `main.py` 中实例化并 `agent.register_skill(skill)`
- **初始化**：首次执行时自动触发（懒初始化），或手动调用 `skill.initialize()`
- **执行**：每次用户消息触发时调用，接收 `SkillContext`，返回 `SkillResult`
- **清理**：Agent 退出时自动调用，释放资源

### 2.2 最小 Skill 模板

创建文件 `src/skills/weather_skill.py`：

```python
from .base import SkillBase, SkillMeta, SkillResult, SkillContext


class WeatherSkill(SkillBase):

    def __init__(self, api_key: str = ""):
        meta = SkillMeta(
            name="weather",
            description="查询天气信息",
            version="1.0.0",
            dependencies=["requests"],
            author="your-name"
        )
        super().__init__(meta)
        self.api_key = api_key

    def initialize(self) -> None:
        """可选：加载模型、建立连接等"""
        self._initialized = True

    def execute(self, context: SkillContext) -> SkillResult:
        """必须实现：核心逻辑"""
        user_input = context.user_input

        try:
            # 你的业务逻辑
            weather_info = f"模拟天气数据：{user_input}"

            # 关键：返回 data["prompt"] 会替换用户输入发给 LLM
            return SkillResult(
                success=True,
                data={
                    "prompt": f"根据天气数据回答用户问题：\n"
                              f"天气数据：{weather_info}\n"
                              f"用户问题：{user_input}"
                },
                message="成功获取天气信息"
            )
        except Exception as e:
            return SkillResult(
                success=False,
                error=str(e),
                message="获取天气信息失败"
            )

    def cleanup(self) -> None:
        """可选：释放资源"""
        self._initialized = False
```

### 2.3 在 main.py 中注册

```python
from src.skills.weather_skill import WeatherSkill

# 创建并注册
weather_skill = WeatherSkill(api_key="your-weather-api-key")
agent.register_skill(weather_skill)
```

### 2.4 SkillResult 的约定

`SkillResult` 的 `data` 字段有以下约定：

| data 键 | 含义 | 是否必须 |
|---------|------|----------|
| `prompt` | 处理后的完整 prompt，会替换用户原始输入 | 推荐 |
| 其他键 | 你自定义的元数据，不会被 Agent 使用 | 可选 |

**如果没有返回 `data["prompt"]**，Agent 会使用用户的原始输入继续对话。

### 2.5 SkillContext 可用字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `trace_id` | `str` | 当前请求的追踪 ID，用于日志串联 |
| `user_input` | `str` | 用户的原始输入文本 |
| `conversation_history` | `List[dict]` | 完整对话历史，格式为 `[{"role": "user/assistant", "content": "..."}]` |
| `config` | `dict` | 透传的配置信息 |
| `extra` | `dict` | 扩展字段 |

---

## 3 扩展方式二：新增 RAG 策略

RAG 管线的每个阶段都使用策略模式，你可以为任何阶段添加新策略。

### 3.1 策略目录结构

```
src/rag/strategies/
├── splitter/      # 分块策略
│   ├── base.py    # BaseSplitter
│   ├── fixed.py   # 固定长度
│   ├── recursive.py
│   ├── semantic.py
│   └── markdown.py
├── index/         # 索引策略
│   ├── base.py    # BaseIndexStrategy
│   ├── dense.py   # 向量索引
│   ├── sparse.py  # BM25
│   └── hybrid.py  # 混合
├── retriever/     # 检索策略
│   ├── base.py    # BaseRetrieverStrategy（定义 SearchResult）
│   ├── top_k.py
│   ├── hybrid.py
│   ├── parent_doc.py
│   └── hyde.py
├── reranker/      # 重排序策略
│   ├── base.py
│   ├── none.py
│   ├── cross_encoder.py
│   └── llm.py
└── query/         # 查询增强策略
    ├── base.py
    ├── none.py
    ├── rewrite.py
    ├── decompose.py
    └── expand.py
```

### 3.2 添加新策略的步骤

以新增一个 `splitter` 策略为例：

**第一步**：在对应目录下创建策略文件 `src/rag/strategies/splitter/sentence.py`：

```python
from .base import BaseSplitter
from ...a_loader import Document


class SentenceSplitter(BaseSplitter):
    """按句子切分"""

    def split(self, text: str) -> list[str]:
        # 实现你的切分逻辑
        sentences = text.split("。")
        return [s.strip() for s in sentences if s.strip()]

    def split_document(self, document: Document) -> list[dict]:
        chunks = self.split(document.content)
        return [
            {"content": chunk, "metadata": {**document.metadata, "chunk_index": i}}
            for i, chunk in enumerate(chunks)
        ]
```

**第二步**：在对应的工厂文件中注册（如 `src/rag/b_splitter.py`）：

找到策略映射字典，添加新条目：

```python
STRATEGY_MAP = {
    "fixed": FixedSplitter,
    "recursive": RecursiveSplitter,
    "semantic": SemanticSplitter,
    "markdown": MarkdownSplitter,
    "sentence": SentenceSplitter,   # 新增
}
```

**第三步**：在 `config/rag.yaml` 中使用：

```yaml
splitter:
  type: "sentence"
  params: {}
```

**第四步**：如果策略有特有的参数，在 `src/config.py` 的对应 Config 类中添加字段（用 `Dict[str, Any]` 通配也可以）。

### 3.3 SearchResult 数据模型

检索和重排序策略都围绕 `SearchResult`（定义在 `src/rag/strategies/retriever/base.py`）工作：

```python
class SearchResult:
    content: str       # 文本内容
    score: float       # 相关性分数
    source: str        # 来源标识
    metadata: dict     # 元数据
```

你的检索策略应返回 `List[SearchResult]`，重排序策略接收并返回同类型。

---

## 4 扩展方式三：添加 Prompt 模板

`PromptManager`（`src/agent/prompt_template.py`）管理所有 Prompt 模板。

### 4.1 添加模板

在 `PromptManager.DEFAULT_TEMPLATES` 中添加：

```python
"my_template": PromptTemplate(
    name="my_template",
    template="""你是{role}。
请回答以下问题：{query}
背景信息：{context}""",
    variables=["role", "query", "context"],
    description="自定义模板"
)
```

### 4.2 在 Skill 中使用

```python
prompt = self.prompt_manager.render(
    "my_template",
    role="专业顾问",
    query=context.user_input,
    context="背景数据..."
)
```

### 4.3 运行时注册

也可以不修改源码，在 Skill 的 `initialize()` 中动态注册：

```python
self.prompt_manager.register(PromptTemplate(
    name="dynamic_template",
    template="...",
    variables=[...]
))
```

---

## 5 扩展方式四：添加 CLI 命令

在 `main.py` 的对话循环 `while True` 中添加新的 `elif` 分支：

```python
elif user_input.startswith("/mycommand "):
    param = user_input[len("/mycommand "):].strip()
    skill = agent.registry.get("my_skill")
    if skill:
        result = skill.my_method(param)
        console.print(f"[green]{result.message}[/green]")
    continue
```

### 5.1 现有命令的处理模式

| 命令 | 处理方式 | 特点 |
|------|----------|------|
| `/rag` | 切换 `active_skills` 列表 | 开关型，无参数 |
| `/ingest <path>` | 通过 `registry.get("rag")` 获取实例 | 调用 Skill 的自有方法 |
| `/stats` | 通过 `registry.get("rag")` 获取实例 | 查询型，用 Rich Table 展示 |
| `/skills` | 通过 `agent.list_skills()` | 通用型，遍历所有 Skill |
| `/clear` | 调用 `agent.clear_history()` | Agent 核心方法 |
| `/quit` | 调用 `agent.cleanup()` 后退出 | 清理所有 Skill 资源 |

---

## 6 配置系统扩展

### 6.1 配置加载流程

```
YAML 文件 → yaml.safe_load() → Pydantic Model → 校验通过 → 使用
```

### 6.2 添加新的配置项

如果新增的 Skill 需要配置，按以下步骤操作：

**第一步**：在 `src/config.py` 中添加配置模型：

```python
class MySkillConfig(BaseModel):
    """MySkill 配置"""
    api_url: str = "https://api.example.com"
    timeout: int = 30
    params: Dict[str, Any] = Field(default_factory=dict)
```

**第二步**：创建配置文件 `config/my_skill.yaml`。

**第三步**：在 Skill 中加载配置：

```python
class MySkill(SkillBase):
    def __init__(self, config_path: str = "config/my_skill.yaml"):
        meta = SkillMeta(name="my_skill", description="...", version="1.0.0")
        super().__init__(meta)

        path = Path(config_path)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            self.config = MySkillConfig(**data.get("my_skill", {}))
        else:
            self.config = MySkillConfig()
```

### 6.3 敏感信息处理

**永远不要**在 YAML 配置文件中存放 API Key、密码等敏感信息。统一使用环境变量：

```python
import os
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("MY_SERVICE_API_KEY")
```

并在 `.env.example` 中添加模板：

```env
MY_SERVICE_API_KEY=your-key-here
```

---

## 7 代码规范

### 7.1 命名规范

| 类型 | 风格 | 示例 |
|------|------|------|
| 函数/变量 | `snake_case` | `load_config()`、`user_input` |
| 类名 | `PascalCase` | `SkillBase`、`RAGConfig` |
| 常量 | `UPPER_CASE` | `MAX_TOKENS`、`DEFAULT_TIMEOUT` |
| 私有属性/方法 | 前缀 `_` | `_initialized`、`_build_context()` |

**禁止使用** `tmp`、`data`、`x`、`result`（单独使用时）等无意义命名。

### 7.2 导入顺序

```python
# 第一组：标准库
import os
import sys

# 第二组：第三方库
from anthropic import Anthropic
from pydantic import BaseModel

# 第三组：本地模块
from src.config import Config
from src.utils.logger import get_logger
```

- 禁止 `from module import *`
- 本地模块使用相对导入：`from .base import SkillBase`

### 7.3 类型注解

所有函数必须有类型注解：

```python
def execute(self, context: SkillContext) -> SkillResult:
    ...

def _build_context(self, results: List[SearchResult], max_length: int) -> str:
    ...
```

### 7.4 注释规范

- **不写**：解释代码"做了什么"的注释（代码本身应该自解释）
- **写**：解释"为什么这样做"的注释，尤其是隐藏约束、workaround、反直觉逻辑

```python
# 不好
# 遍历结果列表
for result in results:
    ...

# 好
# 使用 content 去重而非 doc_id，因为同一文档可能被不同策略重复检索
seen = set()
for result in results:
    if result.content not in seen:
        ...
```

### 7.5 错误处理

```python
# 禁止：吞掉所有异常
except Exception:
    pass

# 禁止：只 print 不记录
except FileNotFoundError as e:
    print(f"文件不存在: {e}")

# 正确：捕获具体异常 + 用 logger 记录
except FileNotFoundError as e:
    self.log.error(f"配置文件不存在: {e}")
    raise
```

### 7.6 日志规范

使用 `loguru`，通过 `get_logger(trace_id)` 获取带追踪 ID 的 logger。

**写日志的位置**：
- 框架入口层（`Agent.chat`、`SkillRegistry.execute`）— 必须记录
- 关键业务节点（文档导入成功、检索结果数等）— 适量记录
- 内部逻辑（数据处理、字符串拼接等）— 不写日志

**禁止**：
- 在循环内写 INFO 级别日志
- 临时调试日志留在代码中（用完即删，可标记 `# TEMP:` 或 `# DEBUG:`）
- 在日志中输出 API Key 等敏感信息（已有 `sanitize_message()` 脱敏函数）

---

## 8 项目目录与模块职责

```
src/
├── __init__.py
├── config.py                 # 所有 Pydantic 配置模型
│
├── agent/                    # Agent 核心（不依赖具体 Skill）
│   ├── __init__.py
│   ├── core.py               # Agent 类：对话引擎、Skill 调度
│   ├── skill_registry.py     # SkillRegistry 单例：注册/发现/执行
│   └── prompt_template.py    # Prompt 模板管理器
│
├── skills/                   # Skill 实现
│   ├── __init__.py           # 导出 SkillBase 和具体 Skill
│   ├── base.py               # 抽象基类 + 数据模型
│   ├── rag_skill.py          # RAG Skill（六阶段管线封装）
│   └── [你的_skill.py]       # 新增 Skill 放这里
│
├── rag/                      # RAG 六阶段管线
│   ├── a_loader.py           # 1. 文档加载（PDF/MD/TXT/DOCX）
│   ├── b_splitter.py         # 2. 文本切分（策略工厂）
│   ├── c_indexer.py          # 3. 向量索引（策略工厂）
│   ├── d_retriever.py        # 4. 检索（策略工厂）
│   ├── e_query_enhancer.py   # 5. 查询增强（策略工厂）
│   ├── f_reranker.py         # 6. 重排序（策略工厂）
│   └── strategies/           # 各阶段的具体策略实现
│       ├── splitter/         #   fixed, recursive, semantic, markdown
│       ├── index/            #   dense, sparse, hybrid
│       ├── retriever/        #   top_k, hybrid, parent_doc, hyde
│       ├── reranker/         #   none, cross_encoder, llm
│       └── query/            #   none, rewrite, decompose, expand
│
└── utils/                    # 工具函数
    ├── __init__.py
    ├── logger.py             # loguru 日志系统、trace_id、脱敏
    └── helpers.py            # 路径工具、目录工具
```

### 8.1 模块依赖规则

```
main.py → agent/ → skills/ → rag/
                  ↘          ↘
                   config/   utils/
```

- `agent/` 可以依赖 `skills/base.py`（抽象层），但**不能**依赖具体 Skill 实现
- `skills/` 可以依赖 `rag/`、`config/`、`utils/`
- `rag/` 可以依赖 `config/`、`utils/`
- **禁止循环依赖**

---

## 9 完整示例：开发一个 Web Search Skill

以下是一个完整的 Skill 开发示例，展示从创建到注册的全流程。

### 9.1 创建 Skill 文件

`src/skills/web_search_skill.py`：

```python
"""Web Search Skill

通过搜索引擎检索信息并注入上下文。
"""

import os
import requests
from typing import Optional

from .base import SkillBase, SkillMeta, SkillResult, SkillContext
from ..agent.prompt_template import get_prompt_manager
from ..utils.logger import get_logger


class WebSearchSkill(SkillBase):
    """Web Search Skill"""

    def __init__(self):
        meta = SkillMeta(
            name="web_search",
            description="联网搜索：从搜索引擎获取实时信息",
            version="1.0.0",
            dependencies=["requests"],
            author="developer"
        )
        super().__init__(meta)

        self.api_key: Optional[str] = None
        self.search_url: str = "https://api.example.com/search"
        self.log = get_logger()
        self.prompt_manager = get_prompt_manager()

    def initialize(self) -> None:
        self.api_key = os.getenv("SEARCH_API_KEY")
        if not self.api_key:
            self.log.warning("SEARCH_API_KEY 未设置，Web Search 功能不可用")
        self._initialized = True

    def execute(self, context: SkillContext) -> SkillResult:
        if not self._initialized:
            self.initialize()

        if not self.api_key:
            return SkillResult(
                success=False,
                error="SEARCH_API_KEY 未配置",
                message="Web Search 不可用"
            )

        try:
            results = self._search(context.user_input)
            context_text = self._format_results(results)

            if not context_text:
                return SkillResult(
                    success=True,
                    data={"prompt": context.user_input, "has_context": False},
                    message="未找到相关搜索结果"
                )

            prompt = self.prompt_manager.render(
                "rag",  # 复用 RAG 模板
                context=context_text,
                query=context.user_input
            )

            return SkillResult(
                success=True,
                data={"prompt": prompt, "has_context": True, "source_count": len(results)},
                message="成功获取搜索结果"
            )

        except Exception as e:
            self.log.error(f"Web Search 失败: {e}")
            return SkillResult(success=False, error=str(e), message="搜索失败")

    def _search(self, query: str) -> list[dict]:
        """调用搜索 API"""
        resp = requests.get(
            self.search_url,
            params={"q": query, "key": self.api_key},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    def _format_results(self, results: list[dict]) -> str:
        """格式化搜索结果"""
        if not results:
            return ""
        parts = []
        for r in results[:5]:
            parts.append(f"[来源: {r.get('url', 'unknown')}]\n{r.get('snippet', '')}")
        return "\n\n---\n\n".join(parts)

    def cleanup(self) -> None:
        self._initialized = False
```

### 9.2 在 main.py 中注册

```python
from src.skills.web_search_skill import WebSearchSkill

web_search_skill = WebSearchSkill()
agent.register_skill(web_search_skill)
```

### 9.3 在 .env 中配置

```env
SEARCH_API_KEY=your-search-api-key
```

### 9.4 添加 CLI 命令（可选）

在 `main.py` 的命令处理中添加：

```python
elif user_input == "/web_search":
    if "web_search" in active_skills:
        active_skills.remove("web_search")
        console.print("[cyan]Web Search 模式已关闭[/cyan]")
    else:
        active_skills.append("web_search")
        console.print("[cyan]Web Search 模式已开启[/cyan]")
    continue
```

### 9.5 更新 __init__.py

在 `src/skills/__init__.py` 中导出：

```python
from .web_search_skill import WebSearchSkill

__all__ = [..., "WebSearchSkill"]
```

---

## 10 测试规范

测试文件放在 `tests/` 目录下。

### 10.1 测试结构

```
tests/
├── test_agent.py          # Agent 核心测试
├── test_skill_registry.py # SkillRegistry 测试
├── test_skills/           # 各 Skill 测试
│   ├── test_rag_skill.py
│   └── test_web_search_skill.py
└── test_rag/              # RAG 各阶段测试
    ├── test_splitter.py
    ├── test_indexer.py
    └── ...
```

### 10.2 Skill 测试模板

```python
import pytest
from src.skills.base import SkillContext


def test_skill_execute():
    skill = MySkill()
    skill.initialize()

    context = SkillContext(
        trace_id="test-001",
        user_input="测试输入",
        conversation_history=[]
    )

    result = skill.execute(context)
    assert result.success
    assert "prompt" in result.data

    skill.cleanup()


def test_skill_not_initialized():
    skill = MySkill()
    context = SkillContext(trace_id="test-002", user_input="测试")
    # SkillRegistry 会自动初始化，但直接调用 execute 前应检查
    result = skill.execute(context)
    # 应该能正常工作（execute 内部有懒初始化逻辑）


def test_skill_error_handling():
    skill = MySkill()
    # 模拟异常输入
    context = SkillContext(trace_id="test-003", user_input="")
    result = skill.execute(context)
    assert isinstance(result.success, bool)
```

---

## 11 技术选型参考

| 场景 | 当前选型 | 替代方案 |
|------|----------|----------|
| LLM API | Anthropic SDK | OpenAI SDK、vLLM、Ollama |
| 日志 | loguru | logging（标准库） |
| 向量数据库 | ChromaDB | FAISS、Milvus、Qdrant |
| Embedding | sentence-transformers | OpenAI Embedding、BGE |
| 配置校验 | Pydantic | dataclasses + 手动校验 |
| CLI 美化 | Rich | 无（纯 print） |
| 文档解析 | pypdf / PyMuPDF | pdfplumber |

---

## 12 开发流程建议

```
需求分析
  ↓
设计 Skill 接口（继承 SkillBase，定义 execute 的输入输出）
  ↓
实现核心逻辑（execute 方法）
  ↓
添加配置项（config/ + src/config.py）
  ↓
编写测试（tests/）
  ↓
在 main.py 中注册并添加 CLI 命令
  ↓
测试完整流程
```
