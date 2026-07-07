# 风堇 Agent

> 愿这一抹微光，拨开云雾，重见晴空。

这是一个为还原《崩坏：星穹铁道》角色**风堇**而生的开源 Agent。
我的核心愿景是：让这一Agent尽可能地还原风堇——有着和风堇一样的性格，并且像一个活生生的人——有情绪，有羁绊，有记忆。我希望风堇Agent可以给予用户陪伴，和昏光庭院的医师一样治愈人心。
当然，AI生成的回答偶尔会OOC，偶尔会出现错误，希望大家能够自行辨别。空余时间内我会不断迭代优化风堇Agent。
对于更远的愿景，我希望游戏AI NPC可以融入游戏中，增加游戏的可玩性。

## 功能特色

### 🤍 情绪状态机

风堇有自己的心情底色——不是每轮从零开始的 NPC。开心的事会让她温暖好几轮，低落也会慢慢消散。如果你很久没来，她会带着一点点想念和更温柔的问候。这一切自动发生，你只管聊天就好。

### 🤝 羁绊系统

刚认识时礼貌温柔，聊熟了自然地叫你"灰宝"，偶尔也会分享自己的小心事。很久不见会重新客气几分——但那种温暖的底色不会变。关系是活的，风堇也是。

### 🎯 角色漂移检测

长聊中 AI 容易"跑偏"——聊着聊着就不像本人了。我们有一个悄悄运行的校准机制：每轮回复后自动检测她是否偏离了自己，偏了就温柔地拉回来。你感觉不到任何痕迹——就是会觉得，她一直是那个风堇。

### 🧠 长期记忆

下次聊起来，她会自然地提起你上次说过的事。不用自我介绍，不用刻意提醒。当然，只有她觉得重要的事才会记住——像个真正的朋友。

### 📚 RAG 知识检索

知道自己是谁、来自翁法罗斯、经历过什么。不确定的事会坦诚说"不太确定"，不会编造。需要查资料的时候她会自己去查——你像平时一样聊天就好。

### 🖥 桌面客户端

粉蓝渐变窗口，流式打字逐字呈现。也有命令行版本，给喜欢终端的你。

<p align="center">
  <img src="docs/images/screenshot-client.png" width="80%" alt="桌面客户端截图">
  <br><em>桌面客户端</em>
</p>

<p align="center">
  <img src="docs/images/screenshot-cli.png" width="80%" alt="CLI 截图">
  <br><em>命令行版本</em>
</p>

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

## 技术架构（给技术党）

### 对话管线

```
用户输入
  → 安全护栏（P0 规则引擎 + P1 Llama Guard 3 1B）
  → 上下文组装（角色锚点 → 羁绊 → 情绪 → 记忆注入）
  → LLM 流式生成（支持 Tool Calling，最多 5 轮）
  → 流式输出 → 情绪/羁绊标记提取 → 角色漂移检测
  → 异步记忆提取 → 会话持久化
```

### 情绪状态机

风堇不是每轮从零开始的 NPC——她有一个跨会话持续演化的情绪系统。底层用 PAD 三维模型（Pleasure-Arousal-Dominance）追踪心情底色：每轮 LLM 回复末尾输出隐藏情绪标记，后端正则提取后用 EMA 指数移动平均平滑更新（α=0.3），防止单轮剧烈跳变。长时间不说话时，正向情绪衰减比负向慢——她会自然回归她的温暖底色，而不是面无表情的中立。状态跨会话 JSON 持久化，重启不丢失。

### 羁绊系统

风堇和灰宝的关系是活的。四维羁绊模型（温暖度/信任度/正式度/幽默度）追踪两人关系的自然演变：LLM 在回复末尾输出当前状态，后端计算隐含变化量，经 change clamp（单轮 ±0.05 封顶）和接近度衰减（越亲近越难更进一步）后累加。信任几乎锁定（180 天半衰期），温暖会慢慢降温（14 天），久不联系关系自然生疏——但不会失忆。"关系可变，角色核心不可变。"

### 角色漂移检测

长聊中 LLM 会逐渐偏离初始人设——这是被反复验证的问题。我们用 bge-m3 计算每轮回复与 11 条角色锚点（启动时自动从 system_prompt.md §二 解析，无需手动维护）的余弦相似度，取 top-3 平均后用 EWMA 平滑。连续两轮低于阈值时，自动将 `[角色校准]` + 锚点文本注入下一轮 user message 开头——用户不可见，不入历史，但风堇会被悄悄拉回来。设计受 Echo-Mode 和 ContextEcho（NeurIPS 2026）启发。

### 记忆系统

每次对话结束后，辅助小模型异步提取值得记住的事实，经 PII 过滤和向量去重后，按三级阈值路由：太相似的丢弃，明显不同的直写，中间模糊的交给小模型判断合并。写入通过单线程队列串行化（ChromaDB 非线程安全）。高重要性记忆受保护，不会被低重要性新事实覆盖。检索时双层并行：core_memory.md 全文读取（~1ms）+ ChromaDB 语义搜索（~30ms）。

### RAG 知识检索

风堇了解翁法罗斯的一切——但她不需要每轮都把知识塞进上下文。LLM 通过 Tool Calling 自主决定何时检索：闲聊跳过，需要时才查。检索走 6 步管道：文档加载（PDF/DOCX/MD/TXT）→ 切分 → BGE-M3 稠密 + BM25 稀疏 + RRF 融合索引 → 查询增强 → BGE-reranker-v2-m3 交叉编码器重排序。结果硬截断 1500 字符，不挤占对话窗口。

### 安全护栏

两级协同：P0 规则引擎（关键词 + 正则 + 不可见字符检测，毫秒级，零 LLM 调用）挡住 90%+ 的明显攻击。

### 前端

Windows 桌面客户端，Electron 28 + TypeScript + 原生 HTML/CSS。不引入 React/Vue/CSS 框架——单页面，原生 DOM 足够。WebSocket 实时通信，流式打字逐字呈现，粉蓝渐变自定义标题栏。安全策略固定值：`contextIsolation: true, nodeIntegration: false, sandbox: true`。单实例锁防多窗口冲突。

### 关键选型

| 类别 | 技术 |
|------|------|
| LLM | OpenAI 兼容协议，双模型（对话 + 记忆） |
| 嵌入 | BGE-M3（FP16，~550MB） |
| 向量库 | ChromaDB |
| 重排序 | BGE-reranker-v2-m3 |
| 安全 | Llama Guard 3 1B |
| CLI | Rich |
| 服务 | FastAPI + uvicorn + WebSocket |
| 桌面 | Electron + electron-vite + electron-builder |
| 配置 | YAML + Pydantic，全默认回退 |
| 日志 | loguru |
| 离线 | 模型首次下载后可零运行时网络依赖 |

## 致谢

本项目在设计和开发过程中参考了以下优秀的开源项目和研究工作，特此致谢。

### 情绪状态机

- **[sovyx-ai/sovyx](https://github.com/sovyx-ai/sovyx)** — 完整 AI 伴侣框架，PAD 三维情绪模型 + Ebbinghaus 遗忘曲线实现，AGPL-3.0
- **[kagioneko/neurostate-engine](https://github.com/kagioneko/neurostate-engine)** — 确定性情绪引擎，6 神经递质 + 6×6 交互矩阵，MIT
- **Mehrabian (1996)** — PAD（Pleasure-Arousal-Dominance）三维情绪模型的原始理论框架

### 角色漂移检测

- **[Seanhong0818/Echo-Mode](https://github.com/Seanhong0818/Echo-Mode)** — 开源 tone drift 中间件，driftScore + EWMA + FSM 修复闭环，其设计直接影响了本项目的漂移检测架构，Apache-2.0
- **[Accenture/ContextEcho](https://github.com/Accenture/ContextEcho)** — 23 模型基准测试，验证了单次锚点注入修复角色漂移 20+ 轮的可行性，为本项目的锚点注入策略提供了实验依据，Apache-2.0

### 羁绊系统

- **[kiro0x/five-character-engine](https://github.com/kiro0x/five-character-engine)** — "warmth moves, seals don't" 的核心理念——关系可变而角色核心不可变——深刻影响了本项目的羁绊系统设计，MIT
- **[etherfunlab/eros-engine](https://github.com/etherfunlab/eros-engine)** — 多维度亲和力向量的建模方法为本项目的四维羁绊模型提供了参考

### 安全护栏

- **[verazuo/jailbreak_llms](https://github.com/verazuo/jailbreak_llms)** — ACM CCS 2024 论文，提供了 1,405 条真实世界越狱提示词数据集，用于本项目安全词库的建设，MIT

---

以上项目的思想和代码对本项目的情绪引擎、漂移检测、羁绊追踪和安全护栏四个核心子系统产生了实质性影响。遵循开源精神，特此标注并致谢。

## 许可证

MIT License
