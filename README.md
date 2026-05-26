# Agent + RAG 项目

一个模块化的 Agent 框架，核心纯净，支持技能插件扩展。

## 项目结构

```
project/
├── src/                  # 源代码
│   ├── agent/            # Agent核心（纯净，不依赖具体Skill）
│   ├── rag/              # RAG模块（示例Skill）
│   ├── skills/           # 技能插件
│   ├── utils/            # 工具函数
│   └── config.py         # 配置管理
├── config/               # 配置文件
│   ├── config.example.yaml  # Agent配置模板
│   └── rag.yaml          # RAG配置
├── tests/                # 测试文件
├── docs/                 # 文档
├── main.py               # CLI入口
├── .env.example          # 环境变量模板
├── README.md             # 本文档
└ requirements.txt        # 依赖列表
└── .gitignore            # Git忽略规则
```

---

## 快速开始

### 1. 克隆项目

```bash
git clone <your-repo-url>
cd 自研_agent+RAG
```

### 2. 配置环境

```bash
# 复制配置模板
cp .env.example .env
cp config/config.example.yaml config/config.yaml

# 编辑 .env，填入API Key
ANTHROPIC_API_KEY=你的真实key
ANTHROPIC_BASE_URL=https://coding.dashscope.aliyuncs.com/apps/anthropic
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 启动

```bash
python main.py
```

---

## CLI 命令

| 命令 | 说明 |
|------|------|
| `/quit` | 退出程序 |
| `/clear` | 清空对话历史 |
| `/rag` | 开启/关闭 RAG 模式 |
| `/ingest <路径>` | 导入文档到知识库 |
| `/stats` | 查看知识库状态 |
| `/skills` | 查看已装配技能 |

---

## 配置说明

### .env（敏感信息）

```bash
ANTHROPIC_API_KEY=your-api-key
ANTHROPIC_BASE_URL=https://coding.dashscope.aliyuncs.com/apps/anthropic
HF_ENDPOINT=https://hf-mirror.com  # 国内镜像
```

### config.yaml（非敏感）

```yaml
agent:
  name: "SimpleAgent"
  model: "glm-5"
  max_tokens: 4096
  temperature: 0.7

system_prompt: |
  你是一个有帮助的AI助手。
```

---

## 扩展指南

### 添加新 Skill

```python
from src.skills.base import SkillBase, SkillMeta, SkillResult, SkillContext

class MySkill(SkillBase):
    def __init__(self):
        meta = SkillMeta(
            name="my_skill",
            description="我的技能",
            version="1.0.0"
        )
        super().__init__(meta)

    def execute(self, context: SkillContext) -> SkillResult:
        # 处理用户输入
        processed_prompt = self._process(context.user_input)

        return SkillResult(
            success=True,
            data={"prompt": processed_prompt}  # 返回处理后的prompt
        )

# 注册到Agent
agent.register_skill(MySkill())

# 使用
agent.chat("你好", skills=["my_skill"])
```

---

## 技术架构

### Agent 核心（纯净胚子）

```
Agent Core（不知道任何具体Skill）
├── SkillRegistry（技能注册）
├── Chat Engine（对话引擎）
└── History Manager（历史管理）
```

### RAG 六层架构

```
a_loader    → 文档加载
b_splitter  → 文本分块
c_indexer   → 向量索引
d_retriever → 检索策略
e_query_enhancer → 查询增强
f_reranker  → 重排序
```

---

## 支持的 LLM 服务

| 服务 | ANTHROPIC_BASE_URL | model |
|------|---------------------|-------|
| 阿里云DashScope | `https://coding.dashscope.aliyuncs.com/apps/anthropic` | glm-5 |
| DeepSeek | `https://api.deepseek.com/v1` | deepseek-chat |
| OpenAI | `https://api.openai.com/v1` | gpt-4o-mini |

---

## 常见问题

### HuggingFace 连接超时

```bash
# .env 中添加
HF_ENDPOINT=https://hf-mirror.com
```

### API Key 无效

检查 `.env` 文件中的 `ANTHROPIC_API_KEY`

### 检索返回 0 结果

降低 `config/rag.yaml` 中的 `score_threshold`

---

## 安全说明

- `.env` 和 `config/config.yaml` 不会提交到 git
- API Key 通过环境变量管理，不写入代码
- 用户数据（向量库、知识库）不会泄露

---

## 许可证

MIT License